"""Reads of receiving (plan §3.4, §4.5): every row through ``visible_to``.

``expected_deliveries`` is what is still coming to one project, for whoever may receive it
there (``receipt.view``): the project's site manager first of all (○ site, Q9), and the
warehouse operators and material managers of the branch that buys for it. Per allocation of
that project on an issued order whose line can still receive, it shows only the fields of
§3.4: order number, supplier display name and expected date, material reference and name,
unit, and the quantity still expected **for that project**. Never a price, the supplier's
contact details, another project's quantities or the rest of the order.

The quantity expected is what a site receipt could still attribute to the allocation: its
ceiling remaining, capped by what the order line can still receive and by its request
line's receipt capacity A - R and outstanding A - D (§4.5).

The screens (M6.3) read the rest from here:

- **Receipts** (``receipts``, ``receipt``, ``receipt_lines``, ``direct_handover``): through
  ``receipt.view``, which reaches a worksite receipt through its project (○ site). Lines
  show what arrived and its attribution, never the order's ordered quantity or price.
- **Finding an order to receive** (``receivable_orders``, ``receipt_order``,
  ``receipt_locations``): through ``receipt.post``. The order of a site manager is found
  through the live allocations of the requests they see (their projects), the same objects
  ``post_receipt`` authorizes at a worksite.
- **The receipt form** (``receipt_rows``): per order line that can still receive, the
  suggested quantity and the allocations that may take it, computed with the same pure
  functions as the service (``attribution``) but without locks: it is a suggestion, the
  service decides. The order line's outstanding quantity is shown only to whoever sees the
  whole order (``po.view``); a site manager gets what their project can take (§3.4).
"""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID

from django.db.models import Q, QuerySet
from django.utils.translation import gettext_lazy as _

from apps.access.context import AccessContext
from apps.access.exceptions import NotInScope
from apps.access.roles import Capability
from apps.handovers.models import Handover
from apps.locations.models import Location
from apps.material_requests import ledger as request_ledger
from apps.material_requests.models import MaterialRequestLine
from apps.projects.models import Project
from apps.purchasing.ledger import book_for_order_lines
from apps.purchasing.models import PurchaseAllocation, PurchaseOrder, PurchaseOrderLine
from apps.purchasing.selectors import Progress, progress_of
from apps.purchasing.waterfall import Book
from apps.receiving.attribution import Candidate, Capacity, default_split
from apps.receiving.models import GoodsReceipt, GoodsReceiptLine, ReceiptAttribution

VIEW, POST, VOID = Capability.RECEIPT_VIEW, Capability.RECEIPT_POST, Capability.RECEIPT_VOID
ZERO = Decimal("0")
DEFAULT_PER_PAGE = 25
RECEIPT_SORTS = {"number": ("number",), "posted": ("received_at",)}
RECEIPT_SORT_LABELS = {"number": _("Receipt"), "posted": _("Received")}
ORDER_SORTS = {"number": ("number",)}
ORDER_SORT_LABELS = {"number": _("Order")}
PROJECT_SORTS = {"code": ("code",), "name": ("name", "code")}
PROJECT_SORT_LABELS = {"code": _("Code"), "name": _("Name")}


@dataclass(frozen=True)
class ExpectedDelivery:
    """One allocation of the project still expecting goods (§3.4's projection)."""

    po_id: UUID
    po_line_id: UUID
    allocation_id: UUID
    order_number: str
    supplier: str
    expected_date: date | None
    material_reference: str
    material_name: str
    unit: str
    request_number: str
    expected: Decimal


def _expected(allocation: PurchaseAllocation, book: Book, flow: request_ledger.Flows) -> Decimal:
    """What a site receipt could still attribute to ``allocation`` (module docstring)."""
    order_line = book.order_lines[allocation.po_line_id]
    approved = allocation.request_line.approved_qty or ZERO
    return min(
        book.allocations[allocation.pk].ceiling_remaining,
        order_line.remaining if order_line.receivable else ZERO,
        approved - flow.received,
        approved - flow.delivered,
    )


def _row(allocation: PurchaseAllocation, expected: Decimal) -> ExpectedDelivery:
    po_line = allocation.po_line
    order = po_line.po
    return ExpectedDelivery(
        po_id=order.pk,
        po_line_id=po_line.pk,
        allocation_id=allocation.pk,
        order_number=order.number or "",
        supplier=str(order.supplier_snapshot.get("name", "")),
        expected_date=order.expected_date,
        material_reference=str(po_line.material_snapshot.get("reference", "")),
        material_name=str(po_line.material_snapshot.get("name", "")),
        unit=po_line.unit.code,
        request_number=allocation.request_line.request.number or "",
        expected=expected,
    )


def _soonest_first(row: ExpectedDelivery) -> tuple[object, ...]:
    return (
        row.expected_date is None,
        row.expected_date or date.min,
        row.order_number,
        row.material_reference,
        row.request_number,
    )


def expected_deliveries(ctx: AccessContext, project_id: UUID) -> list[ExpectedDelivery]:
    """What is still coming to this project, soonest first (404 outside ``receipt.view``)."""
    project = Project.objects.visible_to(ctx, VIEW).filter(pk=project_id).first()
    if project is None:
        raise NotInScope(VIEW)
    lines = (
        MaterialRequestLine.objects.visible_to(ctx, VIEW)
        .filter(request_ledger.open_q(), project=project)
        .values("pk")
    )
    allocations = list(
        PurchaseAllocation.objects.filter(
            request_line__in=lines, qty__gt=0, po_line__po__status=PurchaseOrder.Status.ISSUED
        ).select_related("po_line__po", "po_line__unit", "request_line__request")
    )
    if not allocations:
        return []
    book = book_for_order_lines({allocation.po_line_id for allocation in allocations})
    flows = request_ledger.flows({allocation.request_line_id for allocation in allocations})
    expected = {
        allocation.pk: _expected(allocation, book, flows[allocation.request_line_id])
        for allocation in allocations
    }
    rows = [_row(a, expected[a.pk]) for a in allocations if expected[a.pk] > 0]
    return sorted(rows, key=_soonest_first)


# --- the expected-deliveries screen -----------------------------------------------------------


def expected_projects(ctx: AccessContext, *, q: str = "") -> QuerySet[Project]:
    """Projects with a worksite the user may receive at (``receipt.view``), for the index;
    ``q`` looks at the code and the name."""
    rows = (
        Project.objects.visible_to(ctx, VIEW)
        .filter(worksite__isnull=False, status__in=[Project.Status.ACTIVE, Project.Status.ON_HOLD])
        .select_related("branch__company")
    )
    if q.strip():
        rows = rows.filter(Q(code__icontains=q.strip()) | Q(name__icontains=q.strip()))
    return rows.order_by("code")


def expected_project(ctx: AccessContext, project_id: UUID) -> Project:
    """The project whose expected deliveries are shown (404 outside ``receipt.view``)."""
    found = (
        Project.objects.visible_to(ctx, VIEW)
        .filter(pk=project_id)
        .select_related("branch__company")
        .first()
    )
    if found is None:
        raise NotInScope(VIEW)
    return found


def worksite(ctx: AccessContext, project_id: UUID) -> Location | None:
    """The project's worksite, if the user may post receipts there."""
    return (
        Location.objects.visible_to(ctx, POST)
        .filter(project_id=project_id, kind=Location.Kind.WORKSITE, is_active=True)
        .first()
    )


# --- receipts -------------------------------


def receipts(
    ctx: AccessContext,
    *,
    status: str = "",
    q: str = "",
    location_id: UUID | None = None,
    received_from: date | None = None,
    received_to: date | None = None,
) -> QuerySet[GoodsReceipt]:
    """Receipts the user may see, newest first; ``q`` looks at the receipt's number, the
    order's number and supplier and the delivery note; the rest narrows by place and by the
    (inclusive) days it was received."""
    rows = GoodsReceipt.objects.visible_to(ctx, VIEW).select_related(
        "po", "location", "branch__company", "received_by"
    )
    if status:
        rows = rows.filter(status=status)
    if q:
        rows = rows.filter(
            Q(number__icontains=q)
            | Q(po__number__icontains=q)
            | Q(po__supplier_snapshot__name__icontains=q)
            | Q(delivery_note_ref__icontains=q)
        )
    if location_id is not None:
        rows = rows.filter(location_id=location_id)
    if received_from is not None:
        rows = rows.filter(received_at__date__gte=received_from)
    if received_to is not None:
        rows = rows.filter(received_at__date__lte=received_to)
    return rows


def receipt_places(ctx: AccessContext) -> QuerySet[Location]:
    """The places of the receipts list filter: the ones the user may read receipts of."""
    return Location.objects.visible_to(ctx, VIEW).select_related("branch").order_by("name")


def site_surplus(ctx: AccessContext) -> QuerySet[GoodsReceiptLine]:
    """Receipt lines with a surplus that arrived at a worksite (nobody could take it),
    newest first: it stays on the site until somebody decides what to do with it."""
    return (
        GoodsReceiptLine.objects.visible_to(ctx, VIEW)
        .filter(
            receipt__status=GoodsReceipt.Status.POSTED,
            receipt__location__kind=Location.Kind.WORKSITE,
            surplus_qty__gt=0,
        )
        .select_related("receipt__location__project", "po_line__material", "po_line__unit")
        .order_by("-receipt__received_at", "-id")
    )


def receipt(ctx: AccessContext, receipt_id: UUID) -> GoodsReceipt:
    found = (
        GoodsReceipt.objects.visible_to(ctx, VIEW)
        .filter(pk=receipt_id)
        .select_related("po", "location", "branch__company", "received_by", "voided_by")
        .first()
    )
    if found is None:
        raise NotInScope(VIEW)
    return found


def can_post(ctx: AccessContext) -> bool:
    return ctx.has(POST)


def can_void(ctx: AccessContext, of: GoodsReceipt) -> bool:
    return of.status == GoodsReceipt.Status.POSTED and ctx.can(VOID, of)


@dataclass(frozen=True)
class AttributionRow:
    request_number: str
    project_code: str
    qty: Decimal


@dataclass(frozen=True)
class ReceiptLineRow:
    """A receipt line with the material of the order's snapshot and where it was attributed."""

    line: GoodsReceiptLine
    reference: str
    name: str
    unit: str
    attributions: list[AttributionRow]


def receipt_lines(ctx: AccessContext, of: GoodsReceipt) -> list[ReceiptLineRow]:
    """The receipt's lines in order. No price, and no ordered quantity of the order line."""
    lines = list(
        GoodsReceiptLine.objects.visible_to(ctx, VIEW)
        .filter(receipt=of)
        .select_related("po_line__unit")
        .order_by("po_line__line_no")
    )
    by_line: dict[UUID, list[AttributionRow]] = {line.pk: [] for line in lines}
    attributions = (
        ReceiptAttribution.objects.visible_to(ctx, VIEW)
        .filter(receipt_line__receipt=of)
        .select_related("allocation__request_line__request", "allocation__request_line__project")
        .order_by("created_at", "id")
    )
    for attribution in attributions:
        request_line = attribution.allocation.request_line
        by_line[attribution.receipt_line_id].append(
            AttributionRow(
                request_number=request_line.request.number or "",
                project_code=request_line.project.code,
                qty=attribution.qty,
            )
        )
    return [
        ReceiptLineRow(
            line=line,
            reference=str(line.po_line.material_snapshot.get("reference", "")),
            name=str(line.po_line.material_snapshot.get("name", "")),
            unit=line.po_line.unit.code,
            attributions=by_line[line.pk],
        )
        for line in lines
    ]


def direct_handover(ctx: AccessContext, of: GoodsReceipt) -> Handover | None:
    """The handover a worksite receipt made on the spot, if the user may see handovers."""
    return (
        Handover.objects.visible_to(ctx, Capability.HANDOVER_VIEW).filter(goods_receipt=of).first()
    )


# --- finding the order to receive -------------------------------


def _orders_of_visible_requests(ctx: AccessContext) -> Q:
    """Issued (or closed) orders with live allocations for the request lines the user may
    receive for. A draft or a cancelled order is nobody's to receive."""
    lines = MaterialRequestLine.objects.visible_to(ctx, POST).values("pk")
    served = PurchaseAllocation.objects.filter(
        qty__gt=0,
        request_line__in=lines,
        po_line__po__status__in=[PurchaseOrder.Status.ISSUED, PurchaseOrder.Status.CLOSED],
    )
    return Q(pk__in=served.values("po_line__po_id"))


def receivable_orders(ctx: AccessContext, *, q: str = "") -> QuerySet[PurchaseOrder]:
    """Issued orders of the user's branches (``receipt.post``), newest first.

    ``q`` looks at the order's number and supplier and at the code or name of a project it
    serves. Receipts of a project's worksite start from the expected deliveries instead.
    """
    rows = PurchaseOrder.objects.visible_to(ctx, POST).filter(status=PurchaseOrder.Status.ISSUED)
    if q:
        rows = rows.filter(
            Q(number__icontains=q)
            | Q(supplier_snapshot__name__icontains=q)
            | Q(lines__allocations__request_line__project__code__icontains=q)
            | Q(lines__allocations__request_line__project__name__icontains=q)
        ).distinct()
    return rows.select_related("branch__company", "ship_to").order_by("-issued_at", "-id")


def receipt_progress(orders: Collection[PurchaseOrder]) -> dict[UUID, Progress]:
    """How much of each (already visible) order has arrived."""
    ids = [found.pk for found in orders]
    by_order: dict[UUID, list[UUID]] = {}
    for line_id, po_id in PurchaseOrderLine.objects.filter(po_id__in=ids).values_list(
        "pk", "po_id"
    ):
        by_order.setdefault(po_id, []).append(line_id)
    book = book_for_order_lines([line_id for line_ids in by_order.values() for line_id in line_ids])
    return {
        po_id: progress_of(book.order_line_totals(line_id) for line_id in line_ids)
        for po_id, line_ids in by_order.items()
    }


def receipt_order(ctx: AccessContext, po_id: UUID) -> PurchaseOrder:
    """An order the user may receive goods of: one of their branches, or an issued or closed
    one that buys for a project they see (404 otherwise)."""
    found = (
        PurchaseOrder.objects.filter(pk=po_id)
        .filter(
            Q(pk__in=PurchaseOrder.objects.visible_to(ctx, POST).values("pk"))
            | _orders_of_visible_requests(ctx)
        )
        .select_related("branch__company", "ship_to")
        .first()
    )
    if found is None:
        raise NotInScope(POST)
    return found


def shows_whole_order(ctx: AccessContext, of: PurchaseOrder) -> bool:
    """Whether the user may see the order itself (``po.view``): its outstanding quantities."""
    return ctx.can(Capability.PO_VIEW, of)


def receipt_locations(ctx: AccessContext, of: PurchaseOrder) -> list[Location]:
    """Where goods of this order may arrive and the user may post (C7, S9).

    The order's warehouses (its branch's, or those of the branches it manages) and the
    worksite of each active project with live allocations on it; active places of active
    branches. The service checks again under its locks.
    """
    places = Location.objects.visible_to(ctx, POST).filter(
        is_active=True, branch__is_active=True, branch__company__is_active=True
    )
    warehouses = Q(kind=Location.Kind.WAREHOUSE) & (
        Q(branch_id=of.branch_id) | Q(branch__managed_by_id=of.branch_id)
    )
    served = PurchaseAllocation.objects.filter(po_line__po=of, qty__gt=0)
    worksites = (
        Q(kind=Location.Kind.WORKSITE)
        & Q(project__in=served.values("request_line__project_id"))
        & ~Q(project__status=Project.Status.CLOSED)
    )
    return list(
        places.filter(warehouses | worksites)
        .select_related("branch__company", "project")
        .order_by("kind", "branch__name", "name")
    )


# --- the receipt form -------------------------------


@dataclass(frozen=True)
class SplitChoice:
    """An allocation that may take goods of a line: what it can take and the default share."""

    allocation_id: UUID
    request_number: str
    project_code: str
    cap: Decimal
    suggested: Decimal


@dataclass(frozen=True)
class ReceiptRow:
    """An order line that can still receive, as the form offers it.

    ``remaining`` is the line's outstanding quantity, ``None`` for who does not see the whole
    order; ``suggested`` is the prefilled accepted quantity: everything outstanding at a
    warehouse, what the project's allocations can take at a worksite.
    """

    po_line: PurchaseOrderLine
    reference: str
    name: str
    unit: str
    remaining: Decimal | None
    suggested: Decimal
    splits: list[SplitChoice]


@dataclass
class _FormFacts:
    book: Book
    capacity: Capacity
    labels: dict[
        UUID, tuple[str, str, UUID]
    ]  # request line → request number, project code, project


def _form_facts(book: Book, site: bool) -> _FormFacts:
    request_lines = list(book.request_lines)
    flows = request_ledger.flows(request_lines)
    labels = {
        pk: (number or "", code, project_id)
        for pk, number, code, project_id in MaterialRequestLine.objects.filter(
            pk__in=request_lines
        ).values_list("pk", "request__number", "project__code", "project_id")
    }
    return _FormFacts(
        book=book,
        capacity=Capacity(
            {pk: line.approved for pk, line in book.request_lines.items()},
            {pk: flows[pk].received for pk in request_lines},
            {pk: flows[pk].delivered for pk in request_lines},
            site=site,
        ),
        labels=labels,
    )


def _candidates(facts: _FormFacts, po_line_id: UUID, project_id: UUID | None) -> list[Candidate]:
    """The allocations that may take goods of this line, in attribution order (§4.5)."""
    book = facts.book
    return [
        Candidate(a.id, a.request_line_id, a.ceiling_remaining)
        for a in book.of_order_line(po_line_id)
        if book.request_lines[a.request_line_id].is_open
        and a.ceiling_remaining > 0
        and (project_id is None or facts.labels[a.request_line_id][2] == project_id)
    ]


def _receipt_row(
    po_line: PurchaseOrderLine, facts: _FormFacts, location: Location, *, whole_order: bool
) -> ReceiptRow | None:
    remaining = facts.book.order_lines[po_line.pk].remaining
    candidates = _candidates(facts, po_line.pk, location.project_id)
    if location.project_id is not None and not candidates:
        return None  # a line nobody at this worksite waits for
    caps = {c.allocation_id: facts.capacity.cap(c) for c in candidates}
    split = default_split(remaining, candidates, facts.capacity)
    suggested = sum(split.values(), ZERO) if location.project_id is not None else remaining
    return ReceiptRow(
        po_line=po_line,
        reference=str(po_line.material_snapshot.get("reference", "")),
        name=str(po_line.material_snapshot.get("name", "")),
        unit=po_line.unit.code,
        remaining=remaining if whole_order else None,
        suggested=suggested,
        splits=[
            SplitChoice(
                allocation_id=c.allocation_id,
                request_number=facts.labels[c.request_line_id][0],
                project_code=facts.labels[c.request_line_id][1],
                cap=caps[c.allocation_id],
                suggested=split.get(c.allocation_id, ZERO),
            )
            for c in candidates
        ],
    )


def receipt_rows(ctx: AccessContext, of: PurchaseOrder, location: Location) -> list[ReceiptRow]:
    """The lines of ``of`` that can still receive at ``location``, with the default split.

    Only an issued order receives (C10): a draft, closed or cancelled one has no rows.
    """
    if of.status != PurchaseOrder.Status.ISSUED:
        return []
    lines = list(PurchaseOrderLine.objects.filter(po=of).select_related("unit").order_by("line_no"))
    book = book_for_order_lines([line.pk for line in lines])
    facts = _form_facts(book, site=location.kind == Location.Kind.WORKSITE)
    whole_order = shows_whole_order(ctx, of)
    rows = (
        _receipt_row(line, facts, location, whole_order=whole_order)
        for line in lines
        if book.order_lines[line.pk].has_capacity
    )
    return [row for row in rows if row is not None]
