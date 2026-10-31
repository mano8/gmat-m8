"""Reads of purchasing (plan §3.4, §4.5): every row through ``visible_to``.

An order the user cannot see is ``NotInScope`` (404). Prices reach only ``price.view``
holders on that order: without it a line's price, discount and total are ``None`` (a
warehouse operator sees orders without prices, §8.2). Issued lines show their material
snapshot; drafts show the live catalog.

The **purchase worklist** (§4.5) is for ``po.manage`` holders:

- ``to_purchase``: open ``PURCHASE`` request lines with something left to buy, or with
  demand reserved in draft orders (*reserved in drafts*), in attribution order;
- ``unallocated_on_order``: receivable order lines with quantity on order for nobody, which
  a reconciliation trim or a closure freed: close them short with the supplier or keep the
  goods as stock;
- ``stale_drafts``: drafts older than 14 days still hold their reservations (Q24).
"""

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

from django.db.models import QuerySet
from django.utils import timezone

from apps.access.context import AccessContext
from apps.access.exceptions import NotInScope
from apps.access.roles import Capability
from apps.material_requests import ledger as request_ledger
from apps.material_requests.models import MaterialRequestLine
from apps.purchasing.ledger import book_for_order_lines
from apps.purchasing.models import PurchaseAllocation, PurchaseOrder, PurchaseOrderLine
from apps.purchasing.waterfall import ZERO, OrderLineTotals, RequestLineFacts

VIEW, MANAGE, PRICE = Capability.PO_VIEW, Capability.PO_MANAGE, Capability.PRICE_VIEW
Status = PurchaseOrder.Status
STALE_DRAFT_DAYS = 14  # Q24


# --- orders -----------------------------------------------------------------------------


def orders(
    ctx: AccessContext, *, status: str = "", supplier_id: UUID | None = None
) -> QuerySet[PurchaseOrder]:
    """Orders the user may see, newest first, optionally in one status or to one supplier."""
    rows = PurchaseOrder.objects.visible_to(ctx, VIEW).select_related(
        "branch__company", "supplier", "ship_to"
    )
    if status:
        rows = rows.filter(status=status)
    if supplier_id is not None:
        rows = rows.filter(supplier_id=supplier_id)
    return rows.order_by("-created_at")


def order(ctx: AccessContext, po_id: UUID) -> PurchaseOrder:
    found = orders(ctx).filter(pk=po_id).first()
    if found is None:
        raise NotInScope(VIEW)
    return found


def shows_prices(ctx: AccessContext, of: PurchaseOrder) -> bool:
    return ctx.can(PRICE, of)


@dataclass(frozen=True)
class LineRow:
    """An order line with its ledger; the price fields are ``None`` without ``price.view``."""

    line: PurchaseOrderLine
    reference: str
    name: str
    totals: OrderLineTotals
    unit_price: Decimal | None = None
    discount_pct: Decimal | None = None
    total: Decimal | None = None


def _total(line: PurchaseOrderLine) -> Decimal | None:
    """ordered * price * (1 - discount), to the cent; ``None`` without a price."""
    if line.unit_price is None:
        return None
    factor = 1 - (line.discount_pct or ZERO) / 100
    total: Decimal = line.ordered_qty * line.unit_price * factor
    return total.quantize(Decimal("0.01"))


def lines(ctx: AccessContext, of: PurchaseOrder) -> list[LineRow]:
    """The order's lines in order, with what they ordered, received and have pending."""
    listed = list(
        PurchaseOrderLine.objects.visible_to(ctx, VIEW)
        .filter(po=of)
        .select_related("material", "unit")
        .order_by("line_no")
    )
    book = book_for_order_lines([line.pk for line in listed])
    prices = shows_prices(ctx, of)
    rows = []
    for line in listed:
        snapshot = line.material_snapshot or {}
        rows.append(
            LineRow(
                line=line,
                reference=snapshot.get("reference", line.material.reference),
                name=snapshot.get("name", line.material.name),
                totals=book.order_line_totals(line.pk),
                unit_price=line.unit_price if prices else None,
                discount_pct=line.discount_pct if prices else None,
                total=_total(line) if prices else None,
            )
        )
    return rows


@dataclass(frozen=True)
class AllocationRow:
    """An allocation with the request line it serves and its pending (P) or reservation (Q)."""

    allocation: PurchaseAllocation
    request_number: str
    request_line_no: int
    project_code: str
    attributed: Decimal
    ceiling_remaining: Decimal
    pending: Decimal
    reserved: Decimal


def allocations(ctx: AccessContext, of: PurchaseOrder) -> list[AllocationRow]:
    """The order's allocations, trimmed ones included, line by line in attribution order."""
    found = {
        row.pk: row
        for row in PurchaseAllocation.objects.visible_to(ctx, VIEW)
        .filter(po_line__po=of)
        .select_related("po_line", "request_line__request", "request_line__project")
    }
    numbers = {row.po_line_id: row.po_line.line_no for row in found.values()}
    line_ids = sorted(numbers, key=numbers.__getitem__)
    book = book_for_order_lines(line_ids, empty=True)
    return [
        AllocationRow(
            allocation=found[facts.id],
            request_number=str(found[facts.id].request_line.request.number),
            request_line_no=found[facts.id].request_line.line_no,
            project_code=found[facts.id].request_line.project.code,
            attributed=facts.attributed,
            ceiling_remaining=facts.ceiling_remaining,
            pending=book.pending(facts.id),
            reserved=book.reserved(facts.id),
        )
        for line_id in line_ids
        for facts in book.of_order_line(line_id)
    ]


# --- the purchase worklist ---------------------------------------------------------------


@dataclass(frozen=True)
class WorklistRow:
    """An open line to purchase: approved A, committed, reserved in drafts Q, to purchase."""

    line: MaterialRequestLine
    approved: Decimal
    committed: Decimal
    reserved: Decimal
    to_purchase: Decimal


def _attribution_key(line: MaterialRequestLine) -> tuple[object, ...]:
    """The attribution order of ``waterfall`` (only the request's fields count)."""
    request = line.request
    facts = RequestLineFacts(
        line.pk, request.pk, ZERO, True, request.priority, request.needed_by, request.submitted_at
    )
    return (*facts.order_key, line.pk)


def to_purchase(
    ctx: AccessContext,
    *,
    project_id: UUID | None = None,
    priority: str = "",
    material_id: UUID | None = None,
) -> list[WorklistRow]:
    """Open ``PURCHASE`` lines in ``po.manage`` scope with something to buy or reserved in
    drafts, most urgent first (the attribution order)."""
    rows = (
        MaterialRequestLine.objects.visible_to(ctx, MANAGE)
        .filter(request_ledger.open_q(), sourcing=MaterialRequestLine.Sourcing.PURCHASE)
        .select_related("request", "project", "material", "unit")
    )
    if project_id is not None:
        rows = rows.filter(project_id=project_id)
    if priority:
        rows = rows.filter(request__priority=priority)
    if material_id is not None:
        rows = rows.filter(material_id=material_id)
    found = [
        WorklistRow(
            line=entry.line,
            approved=entry.approved,
            committed=entry.committed,
            reserved=entry.flows.reserved,
            to_purchase=entry.to_purchase,
        )
        for entry in request_ledger.line_ledgers(rows)
        if entry.to_purchase > 0 or entry.flows.reserved > 0
    ]
    return sorted(found, key=lambda row: _attribution_key(row.line))


@dataclass(frozen=True)
class UnallocatedRow:
    line: PurchaseOrderLine
    unallocated: Decimal


def unallocated_on_order(ctx: AccessContext) -> list[UnallocatedRow]:
    """Receivable order lines with quantity on order for nobody (flagged, §4.5)."""
    listed = list(
        PurchaseOrderLine.objects.visible_to(ctx, MANAGE)
        .filter(po__status=Status.ISSUED, closed_short_at__isnull=True)
        .select_related("po", "material", "unit")
        .order_by("po__number", "line_no")
    )
    book = book_for_order_lines([line.pk for line in listed])
    rows = [UnallocatedRow(line, book.order_line_totals(line.pk).unallocated) for line in listed]
    return [row for row in rows if row.unallocated > 0]


def stale_drafts(ctx: AccessContext, *, days: int = STALE_DRAFT_DAYS) -> QuerySet[PurchaseOrder]:
    """Drafts older than ``days``: they reserve demand until a human discards them (Q24)."""
    cutoff = timezone.now() - timedelta(days=days)
    return (
        PurchaseOrder.objects.visible_to(ctx, MANAGE)
        .filter(status=Status.DRAFT, created_at__lt=cutoff)
        .select_related("supplier")
        .order_by("created_at")
    )
