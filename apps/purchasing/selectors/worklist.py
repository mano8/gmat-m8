"""The purchase worklist and the order builder's choices (plan §4.5), for ``po.manage`` holders."""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from django.db.models import (
    BooleanField,
    Case,
    ExpressionWrapper,
    IntegerField,
    Q,
    QuerySet,
    Value,
    When,
)
from django.db.models.functions import Coalesce

from apps.access.context import AccessContext
from apps.core.listing import ListScope, seek_after
from apps.locations.models import Location
from apps.material_requests import ledger as request_ledger
from apps.material_requests.models import MaterialRequestLine
from apps.organizations.models import Branch
from apps.projects.models import Project
from apps.purchasing.ledger import book_for_order_lines
from apps.purchasing.models import PurchaseAllocation, PurchaseOrder, PurchaseOrderLine
from apps.purchasing.selectors.purchase_orders import MANAGE, Status
from apps.purchasing.waterfall import EPOCH, PRIORITY_RANK, ZERO, RequestLineFacts

WORKLIST_PER_PAGE = 25  # materials per page of the worklist (Q41 said 50: see WORKLIST_SCOPE)
# A card per material with its lines on every row: 25 is the most that stays in budget.
WORKLIST_SCOPE = ListScope(max_per_page=25)
UNALLOCATED_STRETCH = 200  # lines read at once for a section that is mostly empty
WORKLIST_STRETCH = 78  # 3 x (page + 1) candidate lines first: one stretch fills most pages
SECTION_PER_PAGE = 10  # drafts, and quantity on order for nobody: fixed, each on its own page
DRAFTS_SCOPE = ListScope(prefix="drafts_", anchor="draft-orders", sized=False)
UNALLOCATED_SCOPE = ListScope(prefix="unallocated_", anchor="ordered-for-nobody", sized=False)


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


ATTRIBUTION_ORDER = ("seek_rank", "seek_undated", "seek_needed", "seek_unsent", "seek_sent", "pk")
"""``RequestLineFacts.order_key`` and the line id in SQL (see ``_in_attribution_order``)."""


def _in_attribution_order(rows: QuerySet[MaterialRequestLine]) -> QuerySet[MaterialRequestLine]:
    """``rows`` with the fields of ``ATTRIBUTION_ORDER``: the waterfall's priority rank, the
    needed-by date (none last) and the submit time (none last), never NULL, so the database
    pages them in exactly the order ``waterfall`` attributes."""
    return rows.annotate(
        seek_rank=Case(
            *(
                When(request__priority=name, then=Value(rank))
                for name, rank in PRIORITY_RANK.items()
            ),
            default=Value(len(PRIORITY_RANK)),
            output_field=IntegerField(),
        ),
        seek_undated=ExpressionWrapper(Q(request__needed_by__isnull=True), BooleanField()),
        seek_needed=Coalesce("request__needed_by", Value(date.min)),
        seek_unsent=ExpressionWrapper(Q(request__submitted_at__isnull=True), BooleanField()),
        seek_sent=Coalesce("request__submitted_at", Value(EPOCH)),
    )


class Worklist:
    """The worklist screen's view of ``to_purchase``, worked out a page at a time.

    ``seek`` reads the next candidate lines in attribution order from the database (one
    ``LIMIT`` query of ids); ``window`` works out the ledgers of the materials of a stretch of
    them and yields each material group at its most urgent line that still needs buying,
    exactly where ``by_material(to_purchase(...))`` puts it. A page costs the groups it shows,
    however long the backlog is (M7d.4).
    """

    def __init__(
        self, ctx: AccessContext, *, project_id: UUID | None = None, priority: str = ""
    ) -> None:
        self._lines = _worklist_lines(ctx, project_id=project_id, priority=priority)
        self._material: dict[UUID, UUID] = {}
        self._groups: dict[UUID, MaterialGroup | None] = {}

    def seek(self, cursor: str, limit: int) -> list[UUID]:
        rows = seek_after(
            _in_attribution_order(self._lines),
            _in_attribution_order(MaterialRequestLine.objects.all()),
            ATTRIBUTION_ORDER,
            cursor,
        )
        found = list(rows.values_list("pk", "material_id")[:limit])
        self._material.update(found)
        return [pk for pk, _ in found]

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


def _receivable_lines(ctx: AccessContext, supplier_id: UUID | None) -> QuerySet[PurchaseOrderLine]:
    lines = PurchaseOrderLine.objects.visible_to(ctx, MANAGE).filter(
        po__status=Status.ISSUED, closed_short_at__isnull=True
    )
    return lines if supplier_id is None else lines.filter(po__supplier_id=supplier_id)


def unallocated_on_order(
    ctx: AccessContext, *, supplier_id: UUID | None = None
) -> list[UnallocatedRow]:
    """Receivable order lines with quantity on order for nobody (flagged, §4.5)."""
    listed = list(
        _receivable_lines(ctx, supplier_id)
        .select_related("po", "material", "unit")
        .order_by("po__number", "line_no")
    )
    book = book_for_order_lines([line.pk for line in listed])
    rows = [UnallocatedRow(line, book.order_line_totals(line.pk).unallocated) for line in listed]
    return [row for row in rows if row.unallocated > 0]


ORDER_LINE_ORDER = ("seek_number", "line_no", "pk")


def _in_number_order(rows: QuerySet[PurchaseOrderLine]) -> QuerySet[PurchaseOrderLine]:
    return rows.annotate(seek_number=Coalesce("po__number", Value("")))


class Unallocated:
    """``unallocated_on_order`` a page at a time (as :class:`Worklist`): the receivable order
    lines in number order, read on from the cursor and worked out in stretches until the page
    is full."""

    def __init__(self, ctx: AccessContext, *, supplier_id: UUID | None = None) -> None:
        self._lines = _receivable_lines(ctx, supplier_id)

    def seek(self, cursor: str, limit: int) -> list[UUID]:
        rows = seek_after(
            _in_number_order(self._lines),
            _in_number_order(PurchaseOrderLine.objects.all()),
            ORDER_LINE_ORDER,
            cursor,
        )
        return list(rows.values_list("pk", flat=True)[:limit])

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
