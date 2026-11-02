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

An order's **supplier dispatches** (M5.3) reach ``po.view`` holders on it (they all hold
``supplier.view``); confirming or resending one is for ``po.manage``.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
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
from apps.audit.models import AuditEvent
from apps.core.listing import ListScope
from apps.locations.models import Location
from apps.material_requests import ledger as request_ledger
from apps.material_requests import selectors as request_selectors
from apps.material_requests.models import MaterialRequest, MaterialRequestLine
from apps.notifications.models import DISPATCH_KINDS, EmailOutbox, Kind
from apps.notifications.models import Status as Dispatch
from apps.organizations.models import Branch
from apps.projects.models import Project
from apps.purchasing.components import TrimReason
from apps.purchasing.ledger import book_for_order_lines
from apps.purchasing.models import PurchaseAllocation, PurchaseOrder, PurchaseOrderLine
from apps.purchasing.waterfall import ZERO, OrderLineTotals, RequestLineFacts

VIEW, MANAGE, PRICE = Capability.PO_VIEW, Capability.PO_MANAGE, Capability.PRICE_VIEW
Status = PurchaseOrder.Status
STALE_DRAFT_DAYS = 14  # Q24
DEFAULT_PER_PAGE = 25
WORKLIST_PER_PAGE = 50  # materials per page of the worklist (Q41)
SECTION_PER_PAGE = 10  # drafts, and quantity on order for nobody: fixed, each on its own page
DRAFTS_SCOPE = ListScope(prefix="drafts_", anchor="draft-orders", sized=False)
UNALLOCATED_SCOPE = ListScope(prefix="unallocated_", anchor="ordered-for-nobody", sized=False)
ORDER_SORTS = {"number": ("number",), "issued": ("issued_at",)}
ORDER_SORT_LABELS = {"number": _("Order"), "issued": _("Issued")}
NEEDS_ATTENTION = (Dispatch.AMBIGUOUS, Dispatch.QUARANTINED)
NOTICE_PENDING = (Dispatch.QUEUED, Dispatch.SENDING, Dispatch.FAILED)


# --- orders -----------------------------------------------------------------------------


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


# --- the purchase worklist ---------------------------------------------------------------


@dataclass(frozen=True)
class WorklistRow:
    """An open line to purchase: approved A, committed, reserved in drafts Q, to purchase."""

    line: MaterialRequestLine
    approved: Decimal
    committed: Decimal
    reserved: Decimal
    to_purchase: Decimal


def _order_key(
    line_id: UUID, request_id: UUID, priority: str, needed_by: date | None, submitted_at: Any
) -> tuple[object, ...]:
    """The attribution order of ``waterfall`` (only the request's fields count)."""
    facts = RequestLineFacts(line_id, request_id, ZERO, True, priority, needed_by, submitted_at)
    return (*facts.order_key, line_id)


def _attribution_key(line: MaterialRequestLine) -> tuple[object, ...]:
    request = line.request
    return _order_key(
        line.pk, request.pk, request.priority, request.needed_by, request.submitted_at
    )


def _worklist_lines(
    ctx: AccessContext,
    *,
    project_id: UUID | None = None,
    priority: str = "",
    material_id: UUID | None = None,
    branch_id: UUID | None = None,
    line_ids: Iterable[UUID] | None = None,
) -> QuerySet[MaterialRequestLine]:
    """Open ``PURCHASE`` lines in ``po.manage`` scope, narrowed by the worklist's filters."""
    rows = MaterialRequestLine.objects.visible_to(ctx, MANAGE).filter(
        request_ledger.open_q(), sourcing=MaterialRequestLine.Sourcing.PURCHASE
    )
    if project_id is not None:
        rows = rows.filter(project_id=project_id)
    if priority:
        rows = rows.filter(request__priority=priority)
    if material_id is not None:
        rows = rows.filter(material_id=material_id)
    if branch_id is not None:
        rows = rows.filter(request__procurement_branch_id=branch_id)
    if line_ids is not None:
        rows = rows.filter(pk__in=list(line_ids))
    return rows


def _worklist_rows(rows: QuerySet[MaterialRequestLine]) -> list[WorklistRow]:
    """The lines of ``rows`` with something to buy or reserved in drafts, most urgent first."""
    found = [
        WorklistRow(
            line=entry.line,
            approved=entry.approved,
            committed=entry.committed,
            reserved=entry.flows.reserved,
            to_purchase=entry.to_purchase,
        )
        for entry in request_ledger.line_ledgers(
            rows.select_related("request", "project", "material", "unit")
        )
        if entry.to_purchase > 0 or entry.flows.reserved > 0
    ]
    return sorted(found, key=lambda row: _attribution_key(row.line))


def to_purchase(
    ctx: AccessContext,
    *,
    project_id: UUID | None = None,
    priority: str = "",
    material_id: UUID | None = None,
    branch_id: UUID | None = None,
    line_ids: Iterable[UUID] | None = None,
) -> list[WorklistRow]:
    """Open ``PURCHASE`` lines in ``po.manage`` scope with something to buy or reserved in
    drafts, most urgent first (the attribution order).

    ``branch_id`` keeps the lines a branch buys for (their procurement branch, C5);
    ``line_ids`` the chosen ones. All of them are worked out: the worklist screen reads
    ``Worklist`` instead.
    """
    return _worklist_rows(
        _worklist_lines(
            ctx,
            project_id=project_id,
            priority=priority,
            material_id=material_id,
            branch_id=branch_id,
            line_ids=line_ids,
        )
    )


@dataclass(frozen=True)
class MaterialGroup:
    """The worklist by material: every line to buy of one material and what it adds up to."""

    reference: str
    name: str
    unit: str
    rows: list[WorklistRow]

    @property
    def to_purchase(self) -> Decimal:
        return sum((row.to_purchase for row in self.rows), ZERO)


def by_material(rows: Iterable[WorklistRow]) -> list[MaterialGroup]:
    """Group worklist rows by material, most urgent line first (a line to purchase always
    names a catalog material, C9)."""
    groups: dict[UUID | None, MaterialGroup] = {}
    for row in rows:
        line = row.line
        if line.material_id not in groups:
            groups[line.material_id] = MaterialGroup(
                reference=line.material.reference if line.material else "",
                name=line.material.name if line.material else line.free_text,
                unit=line.unit.code,
                rows=[],
            )
        groups[line.material_id].rows.append(row)
    return list(groups.values())


class Worklist:
    """The worklist screen's view of ``to_purchase``, worked out a page at a time.

    ``keys`` lists the candidate lines in attribution order from one light query (ids and the
    request's ordering fields); ``window`` works out the ledgers of the materials in a stretch
    of them and yields each material group at its most urgent line that still needs buying,
    exactly where ``by_material(to_purchase(...))`` puts it. A page costs the groups it
    shows, however long the backlog is (M7d.4).
    """

    def __init__(
        self, ctx: AccessContext, *, project_id: UUID | None = None, priority: str = ""
    ) -> None:
        self._lines = _worklist_lines(ctx, project_id=project_id, priority=priority)
        light = self._lines.values_list(
            "pk",
            "request_id",
            "material_id",
            "request__priority",
            "request__needed_by",
            "request__submitted_at",
        )
        candidates = sorted(
            (_order_key(pk, request_id, rank, needed_by, submitted_at), material_id)
            for pk, request_id, material_id, rank, needed_by, submitted_at in light
        )
        self.keys: list[UUID] = [key[-1] for key, _ in candidates]  # type: ignore[misc]
        self._material = {key[-1]: material_id for key, material_id in candidates}
        self._groups: dict[UUID, MaterialGroup | None] = {}

    def _work_out(self, material_ids: set[UUID]) -> None:
        rows = _worklist_rows(self._lines.filter(material_id__in=material_ids))
        found: dict[UUID, list[WorklistRow]] = {}
        for row in rows:
            found.setdefault(row.line.material_id, []).append(row)  # type: ignore[arg-type]
        for material_id in material_ids:
            self._groups[material_id] = (
                by_material(found[material_id])[0] if material_id in found else None
            )

    def line_window(self, chunk: Sequence[UUID]) -> list[tuple[int, WorklistRow]]:
        """The lines of a stretch that still need buying or are reserved, one by one (the
        dashboard counts lines, not materials)."""
        rows = {row.line.pk: row for row in _worklist_rows(self._lines.filter(pk__in=list(chunk)))}
        return [(index, rows[pk]) for index, pk in enumerate(chunk) if pk in rows]

    def window(self, chunk: Sequence[UUID]) -> list[tuple[int, MaterialGroup]]:
        self._work_out({self._material[pk] for pk in chunk} - self._groups.keys())
        shown = []
        for index, pk in enumerate(chunk):
            group = self._groups[self._material[pk]]
            if group is not None and group.rows[0].line.pk == pk:
                shown.append((index, group))
        return shown


@dataclass(frozen=True)
class UnallocatedRow:
    line: PurchaseOrderLine
    unallocated: Decimal


def unallocated_on_order(
    ctx: AccessContext, *, supplier_id: UUID | None = None
) -> list[UnallocatedRow]:
    """Receivable order lines with quantity on order for nobody (flagged, §4.5)."""
    lines = PurchaseOrderLine.objects.visible_to(ctx, MANAGE).filter(
        po__status=Status.ISSUED, closed_short_at__isnull=True
    )
    if supplier_id is not None:
        lines = lines.filter(po__supplier_id=supplier_id)
    listed = list(lines.select_related("po", "material", "unit").order_by("po__number", "line_no"))
    book = book_for_order_lines([line.pk for line in listed])
    rows = [UnallocatedRow(line, book.order_line_totals(line.pk).unallocated) for line in listed]
    return [row for row in rows if row.unallocated > 0]


class Unallocated:
    """``unallocated_on_order`` a page at a time (as :class:`Worklist`): the receivable order
    lines in number order, worked out in stretches until the page is full."""

    def __init__(self, ctx: AccessContext, *, supplier_id: UUID | None = None) -> None:
        lines = PurchaseOrderLine.objects.visible_to(ctx, MANAGE).filter(
            po__status=Status.ISSUED, closed_short_at__isnull=True
        )
        if supplier_id is not None:
            lines = lines.filter(po__supplier_id=supplier_id)
        self.keys: list[UUID] = list(
            lines.order_by("po__number", "line_no", "pk").values_list("pk", flat=True)
        )

    def window(self, chunk: Sequence[UUID]) -> list[tuple[int, UnallocatedRow]]:
        book = book_for_order_lines(chunk)
        free = {
            pk: book.order_line_totals(pk).unallocated if pk in book.order_lines else ZERO
            for pk in chunk
        }
        shown = [pk for pk in chunk if free[pk] > 0]
        listed = {
            line.pk: line
            for line in PurchaseOrderLine.objects.filter(pk__in=shown).select_related(
                "po", "material", "unit"
            )
        }
        return [
            (index, UnallocatedRow(listed[pk], free[pk]))
            for index, pk in enumerate(chunk)
            if pk in listed
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


def dispatches_needing_attention(ctx: AccessContext) -> QuerySet[EmailOutbox]:
    """Supplier dispatches of the user's ``po.manage`` orders a human has to look at: left
    ``AMBIGUOUS`` or ``QUARANTINED``, and cancellation notices not yet sent (M7.2)."""
    return (
        EmailOutbox.objects.filter(
            Q(status__in=NEEDS_ATTENTION) | Q(kind=Kind.PO_CANCELLATION, status__in=NOTICE_PENDING),
            kind__in=DISPATCH_KINDS,
            source_po__in=PurchaseOrder.objects.visible_to(ctx, MANAGE),
        )
        .select_related("source_po")
        .order_by("created_at")
    )


def worklist_projects(ctx: AccessContext) -> QuerySet[Project]:
    """Projects the user buys for: the worklist's project filter."""
    return Project.objects.visible_to(ctx, MANAGE).order_by("code")


# --- the builder --------------------------------------------------------------------------


def ship_to_choices(ctx: AccessContext, *, branch_id: UUID | None = None) -> QuerySet[Location]:
    """Where an order of the user's branches may ship to (plan §4.6, C7).

    A warehouse of the branch or of a branch it manages, or the worksite of an active
    project the branch buys for; ``branch_id`` narrows it to one buying branch. The service
    checks again.
    """
    buying = Branch.objects.visible_to(ctx, MANAGE).filter(is_active=True)
    if branch_id is not None:
        buying = buying.filter(pk=branch_id)
    ids = list(buying.values_list("pk", flat=True))
    return (
        Location.objects.visible_to(ctx, MANAGE)
        .filter(is_active=True, branch__is_active=True)
        .filter(
            Q(kind=Location.Kind.WAREHOUSE)
            & (Q(branch_id__in=ids) | Q(branch__managed_by_id__in=ids))
            | Q(
                kind=Location.Kind.WORKSITE,
                project__procurement_branch_id__in=ids,
                project__status=Project.Status.ACTIVE,
            )
        )
        .select_related("branch__company", "project")
        .order_by("branch__company__code", "branch__code", "kind", "name")
    )


def buyable(ctx: AccessContext, line_ids: Iterable[UUID]) -> list[WorklistRow]:
    """The builder's lines: ticked on the worklist and with something left to buy now (a
    line another draft reserved in full meanwhile is left out)."""
    return [row for row in to_purchase(ctx, line_ids=line_ids) if row.to_purchase > 0]


def candidates(ctx: AccessContext, of: PurchaseOrder) -> list[WorklistRow]:
    """The lines a draft or issued order could still take: to buy now, bought for by its
    branch, for its project when it ships to a worksite, not yet allocated to it."""
    ship_to = of.ship_to
    rows = to_purchase(
        ctx,
        branch_id=of.branch_id,
        project_id=ship_to.project_id if ship_to.kind == Location.Kind.WORKSITE else None,
    )
    taken = set(
        PurchaseAllocation.objects.filter(po_line__po=of, qty__gt=0).values_list(
            "request_line_id", flat=True
        )
    )
    return [row for row in rows if row.to_purchase > 0 and row.line.pk not in taken]


# --- history: revisions, amendments and the reconciliation trims -----------------------------


@dataclass(frozen=True)
class HistoryEntry:
    occurred_at: datetime
    actor: str
    label: Any
    detail: str


ACTION_LABELS = {
    "purchase_order.created": _("Draft created"),
    "purchase_order.updated": _("Draft edited"),
    "purchase_order.issued": _("Issued"),
    "purchase_order.amended": _("Amended"),
    "purchase_order.cancelled": _("Cancelled"),
    "purchase_order.discarded": _("Draft discarded"),
    "purchase_order.auto_closed": _("Closed: nothing left to receive"),
    "purchase_order.auto_reopened": _("Reopened"),
    "purchase_order_line.closed_short": _("Line closed short"),
    "purchase_allocation.trimmed": _("Allocation trimmed"),
    "purchase_order.dispatch_started": _("Sending to the supplier"),
    "purchase_order.dispatch_sent": _("Sent to the supplier"),
    "purchase_order.dispatch_failed": _("Sending to the supplier failed"),
    "purchase_order.dispatch_ambiguous": _("Delivery to the supplier uncertain"),
    "purchase_order.dispatch_confirmed": _("Delivery to the supplier confirmed"),
    "purchase_order.dispatch_resent": _("Sent to the supplier again"),
}
TRIM_LABELS = {
    TrimReason.EXCESS_COMMITMENT: _("the request line was already committed elsewhere"),
    TrimReason.REQUEST_LINE_CLOSED: _("the request line was closed short"),
    TrimReason.REQUEST_CLOSED: _("the request was closed"),
    TrimReason.ORDER_LINE_CLOSED: _("the order line was closed short"),
    TrimReason.ORDER_CANCELLED: _("the order was cancelled"),
    TrimReason.DRAFT_DISCARDED: _("the draft was discarded"),
    TrimReason.ORDERED_QTY_REDUCED: _("the ordered quantity was lowered"),
    TrimReason.UNALLOCATED: _("unallocated"),
}


def _quantity(value: Any) -> str:
    return f"{Decimal(str(value)).normalize():f}"


def _amended(data: dict[str, Any]) -> list[str]:
    return [f"#{data.get('revision')}", str(data.get("reason", ""))]


def _trimmed(data: dict[str, Any]) -> list[str]:
    reason = TrimReason(data["reason"]) if data.get("reason") in TrimReason else None
    return [
        f"{data.get('request')} / {data.get('order_line')}: "
        f"{_quantity(data.get('from'))} → {_quantity(data.get('to'))}",
        str(TRIM_LABELS.get(reason, "")) if reason else "",
    ]


def _closed_short(data: dict[str, Any]) -> list[str]:
    return [f"#{data.get('line_no')}", str(data.get("reason", ""))]


def _dispatched(data: dict[str, Any]) -> list[str]:
    notice = data.get("kind") == Kind.PO_CANCELLATION
    return [
        str(_("cancellation notice")) if notice else f"#{data.get('revision')}",
        str(data.get("error") or data.get("note") or ""),
    ]


def _reason_only(data: dict[str, Any]) -> list[str]:
    return [str(data.get("reason", ""))]


DETAILS = {
    "purchase_order.amended": _amended,
    "purchase_allocation.trimmed": _trimmed,
    "purchase_order_line.closed_short": _closed_short,
}


def _detail(action: str, data: dict[str, Any]) -> str:
    """The reason or what changed in a few words; never the raw payload."""
    describe = (
        _dispatched
        if action.startswith("purchase_order.dispatch_")
        else DETAILS.get(action, _reason_only)
    )
    return " · ".join(part for part in describe(data) if part)


# --- supplier dispatches (M5.3) ---------------------------------------------------------


@dataclass(frozen=True)
class DispatchRow:
    """A supplier dispatch of an order; ``decidable`` ones wait for a human (plan §5)."""

    dispatch: EmailOutbox
    notice: bool  # the cancellation notice, not a revision of the order
    revision: int | None
    decidable: bool
    resendable: bool

    @property
    def confirmable(self) -> bool:
        return self.dispatch.status == Dispatch.AMBIGUOUS

    @property
    def actionable(self) -> bool:
        return self.resendable or self.confirmable


def _dispatches(ctx: AccessContext) -> QuerySet[EmailOutbox]:
    return EmailOutbox.objects.filter(
        kind__in=DISPATCH_KINDS, source_po__in=PurchaseOrder.objects.visible_to(ctx, VIEW)
    )


def dispatch(ctx: AccessContext, dispatch_id: UUID) -> EmailOutbox:
    found = _dispatches(ctx).filter(pk=dispatch_id).select_related("source_po").first()
    if found is None:
        raise NotInScope(VIEW)
    return found


def dispatches(ctx: AccessContext, of: PurchaseOrder) -> list[DispatchRow]:
    """The order's dispatches, newest first, with what a human may still do with each."""
    from apps.purchasing.dispatch import DECIDABLE, refusal, revision_of

    rows = []
    for found in _dispatches(ctx).filter(source_po=of).order_by("-created_at"):
        decidable = found.status in DECIDABLE
        rows.append(
            DispatchRow(
                dispatch=found,
                notice=found.kind == Kind.PO_CANCELLATION,
                revision=revision_of(found),
                decidable=decidable,
                resendable=decidable and refusal(of, found) is None,
            )
        )
    return rows


def awaiting_decision(rows: Iterable[DispatchRow]) -> bool:
    return any(row.confirmable for row in rows)


def notice_awaited(of: PurchaseOrder, rows: list[DispatchRow]) -> bool:
    """Cancelled while a dispatch was being sent: the notice follows if that one reaches the
    supplier (it is listed once queued; nothing is owed if the attempt fails)."""
    return (
        of.cancellation_notice_required
        and not any(row.notice for row in rows)
        and any(row.dispatch.status == Dispatch.SENDING for row in rows)
    )


def can_see_history(ctx: AccessContext, of: PurchaseOrder) -> bool:
    """Who changed what and why: for who manages the order or audits it."""
    return ctx.can(MANAGE, of) or ctx.can(Capability.AUDIT_VIEW, of)


def history(ctx: AccessContext, of: PurchaseOrder) -> list[HistoryEntry]:
    """The order's audit events and its allocations' trims, oldest first (else nothing)."""
    if not can_see_history(ctx, of):
        return []
    events = AuditEvent.objects.filter(
        Q(object_type=PurchaseOrder._meta.label_lower, object_id=of.pk)
        | Q(action="purchase_allocation.trimmed", data__order_id=str(of.pk))
    ).order_by("occurred_at", "id")
    return [
        HistoryEntry(
            occurred_at=event.occurred_at,
            actor=event.actor_label,
            label=ACTION_LABELS.get(event.action, event.action),
            detail=_detail(event.action, event.data),
        )
        for event in events
        if event.action in ACTION_LABELS
    ]


# --- a request's trims, for its ledger page ------------------------------------------------


@dataclass(frozen=True)
class RequestTrim:
    """A lowered allocation as the request side sees it: no order, no supplier."""

    occurred_at: datetime
    actor: str
    line_no: int
    before: Decimal
    after: Decimal
    reason: Any


def request_trims(ctx: AccessContext, of: MaterialRequest) -> dict[int, list[RequestTrim]]:
    """The trims of the request's allocations by request line number, oldest first.

    Closures and reconciliation cuts (§4.5) explain why a line holds less than it did; they
    reach who follows the request up (``request_selectors.can_see_history``), else nothing.
    """
    if not request_selectors.can_see_history(ctx, of):
        return {}
    events = AuditEvent.objects.filter(
        action="purchase_allocation.trimmed", data__request_id=str(of.pk)
    ).order_by("occurred_at", "id")
    grouped: dict[int, list[RequestTrim]] = {}
    for event in events:
        data = event.data
        reason = TrimReason(data["reason"]) if data.get("reason") in TrimReason else None
        grouped.setdefault(int(data["request_line"]), []).append(
            RequestTrim(
                occurred_at=event.occurred_at,
                actor=event.actor_label,
                line_no=int(data["request_line"]),
                before=Decimal(str(data["from"])),
                after=Decimal(str(data["to"])),
                reason=TRIM_LABELS.get(reason, "") if reason else "",
            )
        )
    return grouped
