"""Reads of one purchase order and of the lists of orders: every row through ``visible_to``."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from django.db.models import Exists, F, OuterRef, Q, QuerySet
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.access.context import AccessContext
from apps.access.exceptions import NotInScope
from apps.access.roles import Capability
from apps.projects.models import Project
from apps.purchasing.ledger import book_for_order_lines
from apps.purchasing.models import PurchaseAllocation, PurchaseOrder, PurchaseOrderLine
from apps.purchasing.waterfall import ZERO, OrderLineTotals

VIEW, MANAGE, PRICE = Capability.PO_VIEW, Capability.PO_MANAGE, Capability.PRICE_VIEW
Status = PurchaseOrder.Status
STALE_DRAFT_DAYS = 14  # Q24
DEFAULT_PER_PAGE = 25
ORDER_SORTS = {"number": ("number",), "issued": ("issued_at",)}
ORDER_SORT_LABELS = {"number": _("Order"), "issued": _("Issued")}


def orders(
    ctx: AccessContext,
    *,
    status: str = "",
    supplier_id: UUID | None = None,
    project_id: UUID | None = None,
    q: str = "",
) -> QuerySet[PurchaseOrder]:
    """Orders the user may see, newest first, optionally in one status, to one supplier,
    buying for one project or matching a text (number, supplier name or reference)."""
    rows = PurchaseOrder.objects.visible_to(ctx, VIEW).select_related(
        "branch__company", "supplier", "ship_to"
    )
    if status:
        rows = rows.filter(status=status)
    if supplier_id is not None:
        rows = rows.filter(supplier_id=supplier_id)
    if project_id is not None:
        served = PurchaseAllocation.objects.filter(
            po_line__po=OuterRef("pk"), request_line__project_id=project_id
        )
        rows = rows.filter(Exists(served))
    if q.strip():
        rows = rows.filter(
            Q(number__icontains=q.strip())
            | Q(supplier__name__icontains=q.strip())
            | Q(supplier_ref__icontains=q.strip())
        )
    return rows.order_by("-created_at")


def order_projects(ctx: AccessContext) -> QuerySet[Project]:
    """The projects of the orders list filter: the ones the user can see anyway."""
    return Project.objects.visible_to(ctx, Capability.PROJECT_VIEW).order_by("code")


def order(ctx: AccessContext, po_id: UUID) -> PurchaseOrder:
    found = orders(ctx).filter(pk=po_id).first()
    if found is None:
        raise NotInScope(VIEW)
    return found


def order_line(ctx: AccessContext, line_id: UUID) -> PurchaseOrderLine:
    found = (
        PurchaseOrderLine.objects.visible_to(ctx, VIEW)
        .filter(pk=line_id)
        .select_related("po", "unit")
        .first()
    )
    if found is None:
        raise NotInScope(VIEW)
    return found


def allocation(ctx: AccessContext, allocation_id: UUID) -> PurchaseAllocation:
    found = (
        PurchaseAllocation.objects.visible_to(ctx, VIEW)
        .filter(pk=allocation_id)
        .select_related("po_line__po", "po_line__unit")
        .first()
    )
    if found is None:
        raise NotInScope(VIEW)
    return found


def shows_prices(ctx: AccessContext, of: PurchaseOrder) -> bool:
    return ctx.can(PRICE, of)


def can_manage(ctx: AccessContext, of: PurchaseOrder | None = None) -> bool:
    """Whether the user may change orders (``po.manage``): on ``of``, or somewhere."""
    return ctx.can(MANAGE, of) if of is not None else ctx.has(MANAGE)


class Progress(StrEnum):
    """How much of an issued order has arrived: derived, never stored (§4.2)."""

    NOT_RECEIVED = "NOT_RECEIVED"
    PARTIAL = "PARTIAL"
    RECEIVED = "RECEIVED"

    @property
    def label(self) -> Any:
        return {
            Progress.NOT_RECEIVED: _("Not received"),
            Progress.PARTIAL: _("Partly received"),
            Progress.RECEIVED: _("Received"),
        }[self]


def progress_of(totals: Iterable[OrderLineTotals]) -> Progress:
    """Nothing received, something still to receive, or everything received."""
    listed = list(totals)
    if not any(line.received > 0 for line in listed):
        return Progress.NOT_RECEIVED
    if any(line.outstanding > 0 for line in listed):
        return Progress.PARTIAL
    return Progress.RECEIVED


def progress(ctx: AccessContext, listed: Iterable[PurchaseOrder]) -> dict[UUID, Progress]:
    """The receipt progress of these (visible) orders, in one pass over their lines."""
    by_order: dict[UUID, list[UUID]] = {}
    for line_id, po_id in (
        PurchaseOrderLine.objects.visible_to(ctx, VIEW)
        .filter(po__in=[found.pk for found in listed])
        .values_list("pk", "po_id")
    ):
        by_order.setdefault(po_id, []).append(line_id)
    book = book_for_order_lines([line_id for ids in by_order.values() for line_id in ids])
    return {
        po_id: progress_of(book.order_line_totals(line_id) for line_id in ids)
        for po_id, ids in by_order.items()
    }


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

    @property
    def unreserved(self) -> Decimal:
        """Ordered and not (yet) allocated to any request line."""
        return max(ZERO, self.totals.ordered - self.totals.allocated)

    @property
    def final_qty(self) -> Decimal:
        return final_qty(self.line, self.totals.received)


def final_qty(line: PurchaseOrderLine, received: Decimal) -> Decimal:
    """What a line orders in the end: once closed short, what it received."""
    return received if line.closed_short_at is not None else line.ordered_qty


def line_total(line: PurchaseOrderLine, quantity: Decimal | None = None) -> Decimal | None:
    """quantity (the ordered one by default) * price * (1 - discount), to the cent;
    ``None`` without a price."""
    if line.unit_price is None:
        return None
    factor = 1 - (line.discount_pct or ZERO) / 100
    ordered = line.ordered_qty if quantity is None else quantity
    total: Decimal = ordered * line.unit_price * factor
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
        totals = book.order_line_totals(line.pk)
        rows.append(
            LineRow(
                line=line,
                reference=snapshot.get("reference", line.material.reference),
                name=snapshot.get("name", line.material.name),
                totals=totals,
                unit_price=line.unit_price if prices else None,
                discount_pct=line.discount_pct if prices else None,
                total=line_total(line, final_qty(line, totals.received)) if prices else None,
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


def drafts(ctx: AccessContext, *, supplier_id: UUID | None = None) -> QuerySet[PurchaseOrder]:
    """Draft orders in ``po.manage`` scope, oldest first: each one reserves demand (Q)."""
    rows = PurchaseOrder.objects.visible_to(ctx, MANAGE).filter(status=Status.DRAFT)
    if supplier_id is not None:
        rows = rows.filter(supplier_id=supplier_id)
    return rows.select_related("supplier", "ship_to").order_by("created_at")


def is_stale(of: PurchaseOrder, *, days: int = STALE_DRAFT_DAYS) -> bool:
    return of.status == Status.DRAFT and of.created_at < timezone.now() - timedelta(days=days)


def stale_drafts(ctx: AccessContext, *, days: int = STALE_DRAFT_DAYS) -> QuerySet[PurchaseOrder]:
    """Drafts older than ``days``: they reserve demand until a human discards them (Q24)."""
    cutoff = timezone.now() - timedelta(days=days)
    return drafts(ctx).filter(created_at__lt=cutoff)


def awaiting_receipt(ctx: AccessContext) -> QuerySet[PurchaseOrder]:
    """Issued orders still to receive, the earliest expected date first (none last)."""
    return (
        PurchaseOrder.objects.visible_to(ctx, VIEW)
        .filter(status=Status.ISSUED)
        .select_related("supplier")
        .order_by(F("expected_date").asc(nulls_last=True), "number")
    )


def overdue(ctx: AccessContext, *, today: date | None = None) -> QuerySet[PurchaseOrder]:
    """Issued orders whose expected date has passed."""
    return awaiting_receipt(ctx).filter(expected_date__lt=today or timezone.localdate())
