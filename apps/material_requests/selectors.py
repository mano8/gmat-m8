"""Reads for the request screens (plan §3.5, §4.2, M4.3): every row through ``visible_to``.

A request or line the user cannot see is ``NotInScope`` (404). The screens of the
requester show the user's own requests; ``request.view`` decides what else a reviewer may
open. Nothing here returns a price: requests carry none, and the catalog has none.

The ``can_*`` predicates only decide what the page offers (the services check again, in
their transaction): ownership, the state and, for a withdrawal, that no line is decided.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from django.db.models import (
    Case,
    CharField,
    Count,
    Exists,
    F,
    IntegerField,
    Max,
    Min,
    OuterRef,
    Q,
    QuerySet,
    Value,
    When,
)
from django.db.models.functions import Cast, Coalesce, Concat, Lower
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.access.context import AccessContext
from apps.access.exceptions import NotInScope
from apps.access.roles import SELF_DECIDING_ROLES, Capability
from apps.access.scopes import object_in_scope
from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.catalog import selectors as catalog_selectors
from apps.catalog.models import Material
from apps.core.listing import ListScope, seek_after
from apps.material_requests import ledger, reviewers, revisions
from apps.material_requests.models import (
    MaterialRequest,
    MaterialRequestBaseline,
    MaterialRequestBaselineLine,
    MaterialRequestLine,
    MaterialRequestLineRevision,
)
from apps.projects.models import Project

DEFAULT_PER_PAGE = 25
MY_REQUESTS_PER_PAGE = 10
NEW_REQUEST_PER_PAGE = 10
PROJECT_SORTS = {"code": ("code",), "name": ("name", "code")}
PROJECT_SORT_LABELS = {"code": _("Code"), "name": _("Name")}
MY_REQUEST_SORTS = {"number": ("number",), "submitted": ("submitted_at",)}
# A card with every line of the request: 50 of them is the largest page that stays in budget.
MY_REQUESTS_SCOPE = ListScope(max_per_page=50)
MY_REQUEST_SORT_LABELS = {"number": _("Number"), "submitted": _("Submitted")}
INBOX_SORTS = {
    "needed": ("needed_by",),
    "priority": ("priority_rank",),
    "submitted": ("submitted_at",),
}
INBOX_SORT_LABELS = {
    "needed": _("Needed by"),
    "priority": _("Priority"),
    "submitted": _("Submitted"),
}
RECENT_MATERIALS = 8
# How many of the user's latest lines are looked at for the recent materials.
RECENT_WINDOW = 60
# How far back the dashboard looks for closed requests that need attention.
CLOSED_WINDOW_DAYS = 30

VIEW, CREATE, WITHDRAW = (
    Capability.REQUEST_VIEW,
    Capability.REQUEST_CREATE,
    Capability.REQUEST_WITHDRAW,
)
REVIEW, SOURCE, CANCEL_ANY = (
    Capability.REQUEST_REVIEW,
    Capability.REQUEST_SOURCE,
    Capability.REQUEST_CANCEL_ANY,
)
EDIT = Capability.REQUEST_EDIT
Status = MaterialRequest.Status
Decision = MaterialRequestLine.Decision

# The status chips of "My requests" and what each shows (``""`` is all of them).
STATUS_FILTERS = (
    Status.DRAFT,
    Status.SUBMITTED,
    Status.APPROVED,
    Status.REJECTED,
    Status.CLOSED,
    Status.CANCELLED,
)


# --- projects --------------------------------------------------------------------------


def creatable_projects(ctx: AccessContext, *, q: str = "") -> QuerySet[Project]:
    """Projects the user may start a request for, the ones used most recently first.

    Only active projects take new requests (S9). Matches the code, name or client.
    """
    rows = (
        Project.objects.visible_to(ctx, CREATE)
        .filter(status=Project.Status.ACTIVE)
        .select_related("branch__company", "procurement_branch__company")
    )
    q = q.strip()
    if q:
        rows = rows.filter(
            Q(code__icontains=q) | Q(name__icontains=q) | Q(client_name__icontains=q)
        )
    return rows.annotate(
        last_requested=Max("requests__created_at", filter=Q(requests__requested_by_id=ctx.user_id))
    ).order_by(F("last_requested").desc(nulls_last=True), "code")


# --- requests --------------------------------------------------------------------------


def my_requests(
    ctx: AccessContext, *, status: str = "", q: str = "", project_id: UUID | None = None
) -> QuerySet[MaterialRequest]:
    """The user's own requests, newest first, optionally in one status, project or number."""
    rows = (
        MaterialRequest.objects.visible_to(ctx, VIEW)
        .filter(requested_by_id=ctx.user_id)
        .select_related("project")
    )
    if status:
        rows = rows.filter(status=status)
    if project_id is not None:
        rows = rows.filter(project_id=project_id)
    if q.strip():
        rows = rows.filter(number__icontains=q.strip())
    return rows.order_by("-created_at")


def my_request_projects(ctx: AccessContext) -> QuerySet[Project]:
    """The projects the user has made requests for (the filter of "My requests")."""
    own = my_requests(ctx).values("project_id")
    return Project.objects.filter(pk__in=own).order_by("code")


def request(ctx: AccessContext, request_id: UUID) -> MaterialRequest:
    found = (
        MaterialRequest.objects.visible_to(ctx, VIEW)
        .select_related("project", "procurement_branch__company", "requested_by")
        .filter(pk=request_id)
        .first()
    )
    if found is None:
        raise NotInScope(VIEW)
    return found


def line(ctx: AccessContext, line_id: UUID) -> MaterialRequestLine:
    found = (
        MaterialRequestLine.objects.visible_to(ctx, VIEW)
        .select_related("request", "material", "unit")
        .filter(pk=line_id)
        .first()
    )
    if found is None:
        raise NotInScope(VIEW)
    return found


def lines(ctx: AccessContext, of: Sequence[MaterialRequest]) -> dict[UUID, list[ledger.LineLedger]]:
    """The lines of these requests with their ledgers (one query for the flows)."""
    rows = (
        MaterialRequestLine.objects.visible_to(ctx, VIEW)
        .filter(request__in=of)
        .select_related("request", "material", "unit", "decided_by", "closed_short_by")
        .order_by("request_id", "line_no")
    )
    grouped: dict[UUID, list[ledger.LineLedger]] = {item.pk: [] for item in of}
    for entry in ledger.line_ledgers(rows):
        grouped[entry.line.request_id].append(entry)
    return grouped


def line_for_material(
    ctx: AccessContext, of: MaterialRequest, material_id: UUID
) -> MaterialRequestLine | None:
    """The request's line for a catalog material, if it has one (adding it again adds up)."""
    return (
        MaterialRequestLine.objects.visible_to(ctx, VIEW)
        .filter(request=of, material_id=material_id)
        .select_related("request", "material", "unit")
        .first()
    )


# --- materials -------------------------------------------------------------------------


def recent_materials(ctx: AccessContext, *, limit: int = RECENT_MATERIALS) -> list[Material]:
    """The active materials the user requested most recently, newest first."""
    latest = list(
        MaterialRequestLine.objects.visible_to(ctx, VIEW)
        .filter(request__requested_by_id=ctx.user_id, material__isnull=False)
        .order_by()
        .values("material_id")
        .annotate(last_used=Max("created_at"))
        .order_by("-last_used")
        .values_list("material_id", flat=True)[:RECENT_WINDOW]
    )
    found = {
        material.pk: material for material in catalog_selectors.materials(ctx).filter(pk__in=latest)
    }
    return [found[pk] for pk in latest if pk in found][:limit]


# --- what the page offers --------------------------------------------------------------


def _own(ctx: AccessContext, of: MaterialRequest) -> bool:
    return of.requested_by_id == ctx.user_id


def can_edit(ctx: AccessContext, of: MaterialRequest) -> bool:
    """Own draft: lines, review and submit."""
    return _own(ctx, of) and of.status == Status.DRAFT and ctx.can(CREATE, of)


def can_withdraw(ctx: AccessContext, of: MaterialRequest) -> bool:
    """Own submitted request while no reviewer has decided a line."""
    return (
        _own(ctx, of)
        and of.status == Status.SUBMITTED
        and ctx.can(WITHDRAW, of)
        and not MaterialRequestLine.objects.visible_to(ctx, VIEW)
        .filter(request=of)
        .exclude(decision=Decision.PENDING)
        .exists()
    )


# --- the reviewer's inbox and page (M4.4) ----------------------------------------------

# What the inbox lists: requests waiting for a decision, and approved ones to follow up.
INBOX_STATUSES = (Status.SUBMITTED, Status.APPROVED)
# What the inbox's status filter and "Assigned to me" can list (M7b.4, maintainer
# 2026-11-02): rejected requests too, to look back at.
ASSIGNED_STATUSES = (*INBOX_STATUSES, Status.REJECTED)
# Most urgent first, then the earliest needed-by date (none last), then the oldest submit.
PRIORITY_RANK = Case(
    When(priority="URGENT", then=Value(0)),
    When(priority="HIGH", then=Value(1)),
    When(priority="NORMAL", then=Value(2)),
    default=Value(3),
    output_field=IntegerField(),
)


def inbox(
    ctx: AccessContext,
    *,
    status: str = Status.SUBMITTED,
    priority: str = "",
    project_id: UUID | None = None,
    needed_before: date | None = None,
    assigned_to_me: bool = False,
) -> QuerySet[MaterialRequest]:
    """Requests in the user's review scope in one status (submitted unless another of
    ``ASSIGNED_STATUSES`` is asked), the most pressing first.

    ``assigned_to_me`` keeps the ones sent to the user (``assigned_to``, M7b.4).
    """
    rows = (
        assigned_to(ctx, statuses=ASSIGNED_STATUSES)
        if assigned_to_me
        else MaterialRequest.objects.visible_to(ctx, REVIEW)
        .select_related("project", "requested_by")
        .annotate(line_count=Count("lines"))
    ).filter(status=status if status in ASSIGNED_STATUSES else Status.SUBMITTED)
    if priority:
        rows = rows.filter(priority=priority)
    if project_id is not None:
        rows = rows.filter(project_id=project_id)
    if needed_before is not None:
        rows = rows.filter(needed_by__lte=needed_before)
    return rows.annotate(priority_rank=PRIORITY_RANK).order_by(
        "priority_rank", F("needed_by").asc(nulls_last=True), "submitted_at"
    )


# --- reviewer assignment (M7b.2) -------------------------------------------------------


def can_assign(ctx: AccessContext, of: MaterialRequest) -> bool:
    """Sending a submitted or approved request to another reviewer (the service checks again)."""
    return of.status in INBOX_STATUSES and ctx.can(REVIEW, of)


def eligible_reviewers(ctx: AccessContext, of: MaterialRequest) -> list[User]:
    """Who ``of`` can be sent to (the reviewer choices), by name.

    For its requester while it is a draft (judged against the procurement branch its
    submit will take) and for a reviewer who may reassign it; nobody for anyone else.
    """
    if not (can_edit(ctx, of) or can_assign(ctx, of)):
        return []
    return reviewers.candidates(reviewers.target_branch(of))


def assignee(of: MaterialRequest) -> User | None:
    """The request's assigned reviewer while they still qualify; ``None``: unassigned."""
    return reviewers.assignee(of)


def assigned_to(
    ctx: AccessContext, *, statuses: Sequence[str] = INBOX_STATUSES
) -> QuerySet[MaterialRequest]:
    """ "Assigned to me": the user's requests in ``statuses`` (submitted and approved by
    default), the most pressing first.

    Only while a grant through which they may be assigned still covers the request's
    procurement branch: a revoked or expired assignee's list is empty, and the requests
    read as unassigned everywhere else.
    """
    scope = reviewers.assignable_scope(ctx)
    return (
        MaterialRequest.objects.visible_to(ctx, REVIEW)
        .filter(
            scope.branch_q("procurement_branch"),
            assigned_reviewer_id=ctx.user_id,
            status__in=statuses,
        )
        .select_related("project", "requested_by")
        .annotate(line_count=Count("lines"))
        .order_by(PRIORITY_RANK, F("needed_by").asc(nulls_last=True), "submitted_at")
    )


def assigned_ids(ctx: AccessContext, of: Sequence[MaterialRequest]) -> set[UUID]:
    """Which of ``of`` are assigned to the user and still count (as in ``assigned_to``)."""
    return set(
        assigned_to(ctx, statuses=ASSIGNED_STATUSES)
        .filter(pk__in=[found.pk for found in of])
        .values_list("pk", flat=True)
    )


def sourcing_to_decide(ctx: AccessContext) -> QuerySet[MaterialRequestLine]:
    """Open lines of the user's ``request.source`` scope whose sourcing is not decided yet
    (stock or purchase), the longest waiting first (M7.2 dashboard)."""
    return (
        MaterialRequestLine.objects.visible_to(ctx, SOURCE)
        .filter(ledger.open_q(), sourcing=MaterialRequestLine.Sourcing.UNDECIDED)
        .select_related("request", "project", "material", "unit")
        .order_by("request__submitted_at", "request__number", "line_no")
    )


def closed_needing_attention(
    ctx: AccessContext, *, days: int = CLOSED_WINDOW_DAYS
) -> QuerySet[MaterialRequest]:
    """Requests closed in the last ``days`` days with a line closed short: something was
    not delivered, and whoever follows the request up may want to look again (M7.2)."""
    short = MaterialRequestLine.objects.filter(
        request=OuterRef("pk"), closed_short_at__isnull=False
    )
    return (
        MaterialRequest.objects.visible_to(ctx, SOURCE)
        .filter(status=Status.CLOSED, closed_at__gte=timezone.now() - timedelta(days=days))
        .filter(Exists(short))
        .select_related("project")
        .order_by("-closed_at")
    )


def inbox_projects(ctx: AccessContext) -> QuerySet[Project]:
    """The projects of the inbox filter: the ones whose requests the user reviews."""
    return Project.objects.visible_to(ctx, Capability.PROJECT_VIEW).order_by("code")


def link_candidates(ctx: AccessContext, of: MaterialRequestLine, query: str) -> list[Material]:
    """Active catalog materials matching ``query`` in the unit of a non-catalog line.

    Only a material in the line's own unit can be linked (no conversions, Q15).
    """
    return catalog_selectors.search(ctx, query, unit_id=of.unit_id)


def can_review(ctx: AccessContext, of: MaterialRequest) -> bool:
    """Decisions, return for changes and reductions (the services check again)."""
    return ctx.can(REVIEW, of)


def can_decide(ctx: AccessContext, of: MaterialRequest) -> bool:
    """Review, of one's own request only where the procurement branch allows it or a
    self-deciding role's grant covers the request (Q33; the services check again)."""
    if not can_review(ctx, of):
        return False
    if of.requested_by_id != ctx.user_id or of.procurement_branch.allow_self_approval:
        return True
    return object_in_scope(of, ctx.scope_for(REVIEW).of_roles(SELF_DECIDING_ROLES))


def can_edit_lines(ctx: AccessContext, of: MaterialRequest) -> bool:
    """A reviewer's edits of the lines: submitted and approved requests (M7b.3; the
    services check again, the line's state and the ledger floors included)."""
    return of.status in {Status.SUBMITTED, Status.APPROVED} and ctx.can(EDIT, of)


def can_source(ctx: AccessContext, of: MaterialRequest) -> bool:
    """Sourcing, schedule, resolution and closing (the services check again)."""
    return ctx.can(SOURCE, of)


def ships_to_own_worksite(ctx: AccessContext, of: MaterialRequest) -> bool:
    """Whether a live allocation of the request is on an issued order shipping to the
    request's own worksite: after a manual close, goods may still arrive there (plan §4.9;
    they are recorded as site surplus)."""
    return (
        MaterialRequestLine.objects.visible_to(ctx, SOURCE)
        .filter(
            request=of,
            allocations__qty__gt=0,
            allocations__po_line__po__status="ISSUED",
            allocations__po_line__po__ship_to__project_id=of.project_id,
        )
        .exists()
    )


def can_cancel_any(ctx: AccessContext, of: MaterialRequest) -> bool:
    """Cancelling somebody else's request: only before anything is allocated or delivered."""
    return (
        ctx.can(CANCEL_ANY, of)
        and of.requested_by_id != ctx.user_id
        and of.status in {Status.SUBMITTED, Status.APPROVED}
    )


def can_see_history(ctx: AccessContext, of: MaterialRequest) -> bool:
    """The timeline shows who did what and why: for who reviews, sources or audits it."""
    return can_review(ctx, of) or can_source(ctx, of) or ctx.can(Capability.AUDIT_VIEW, of)


ACTION_LABELS = {
    "request.created": _("Draft created"),
    "request.updated": _("Request edited"),
    "request.submitted": _("Submitted"),
    "request.withdrawn": _("Withdrawn"),
    "request.discarded": _("Draft discarded"),
    "request.cancelled": _("Cancelled"),
    "request.returned": _("Returned for changes"),
    "request.approved": _("Approved"),
    "request.rejected": _("Rejected"),
    "request.rescheduled": _("Schedule changed"),
    "request.reviewer_assigned": _("Sent to a reviewer"),
    "request.closed": _("Closed"),
    "request.reopened": _("Reopened"),
    "request.auto_closed": _("Closed automatically"),
    "request.auto_reopened": _("Reopened automatically"),
    "request_line.added": _("Line added"),
    "request_line.updated": _("Line edited"),
    "request_line.removed": _("Line removed"),
    "request_line.decided": _("Line decided"),
    "request_line.reduced": _("Approved quantity reduced"),
    "request_line.sourcing_set": _("Sourcing set"),
    "request_line.resolved": _("Linked to the catalog"),
    "request_line.closed_short": _("Line closed short"),
    "request_line.reopened": _("Line reopened"),
    "request_line.edited": _("Line changed by a reviewer"),
    "request_line.added_by_reviewer": _("Line added by a reviewer"),
    "request_line.removed_by_reviewer": _("Line removed by a reviewer"),
    "request_line.replaced": _("Line replaced by a reviewer"),
    "attachment.added": _("File attached"),
    "attachment.removed": _("File removed"),
}


@dataclass(frozen=True)
class HistoryEntry:
    occurred_at: datetime
    actor: str
    action: str
    label: Any
    line_no: int | None
    detail: str


def _quantity(value: Any) -> str:
    return f"{Decimal(str(value)).normalize():f}"


def _to(changes: dict[str, Any], name: str) -> Any:
    """The new value of a changed field, if it changed."""
    return (changes.get(name) or {}).get("to")


def _join(*parts: str) -> str:
    return " · ".join(part for part in parts if part)


def _decided_detail(data: dict[str, Any], changes: dict[str, Any]) -> str:
    decision, quantity = _to(changes, "decision"), _to(changes, "approved_qty")
    return _join(
        str(MaterialRequestLine.Decision(decision).label) if decision else "",
        _quantity(quantity) if quantity is not None else "",
        _to(changes, "decision_note") or "",
    )


def _reduced_detail(data: dict[str, Any], changes: dict[str, Any]) -> str:
    change = changes.get("approved_qty") or {}
    arrow = f"{_quantity(change['from'])} → {_quantity(change['to'])}" if change else ""
    return _join(arrow, data.get("reason", ""))


def _sourcing_detail(data: dict[str, Any], changes: dict[str, Any]) -> str:
    return str(_to(changes, "sourcing") or "")


def _rescheduled_detail(data: dict[str, Any], changes: dict[str, Any]) -> str:
    return _join(*(f"{name}: {change.get('to') or '—'}" for name, change in changes.items()))


def _attachment_detail(data: dict[str, Any], changes: dict[str, Any]) -> str:
    return _join(data.get("file_name", ""), data.get("reason", ""))


_DETAILS = {
    "attachment.added": _attachment_detail,
    "attachment.removed": _attachment_detail,
    "request_line.decided": _decided_detail,
    "request_line.reduced": _reduced_detail,
    "request_line.sourcing_set": _sourcing_detail,
    "request.rescheduled": _rescheduled_detail,
}


def _detail(action: str, data: dict[str, Any]) -> str:
    """The reason or note of an event, or what it changed; never the raw payload."""
    handler = _DETAILS.get(action)
    if handler is not None:
        return handler(data, data.get("changes") or {})
    return str(data.get("reason") or data.get("note") or "")


def history(ctx: AccessContext, of: MaterialRequest) -> list[HistoryEntry]:
    """The request's audit events, oldest first, for who may see them (else nothing)."""
    if not can_see_history(ctx, of):
        return []
    events = AuditEvent.objects.filter(
        object_type=MaterialRequest._meta.label_lower, object_id=of.pk
    ).order_by("occurred_at", "id")
    return [
        HistoryEntry(
            occurred_at=event.occurred_at,
            actor=event.actor_label,
            action=event.action,
            label=ACTION_LABELS.get(event.action, event.action),
            line_no=event.data.get("line_no"),
            detail=_detail(event.action, event.data),
        )
        for event in events
    ]


# --- reviewer edits: the as-submitted copy and the revisions (M7b.3, Q34) ---------------


@dataclass(frozen=True)
class LineHistory:
    """A line of the request, as its requester submitted it, and the reviewers' edits.

    ``submitted`` is ``None`` for a line a reviewer added (or that replaced another);
    ``revisions`` are oldest first and include the one that created the line and the one
    that replaced it. Nothing beyond what the line itself shows: material, description,
    unit, quantities, decision, who edited it, when and why.
    """

    line: MaterialRequestLine
    submitted: MaterialRequestBaselineLine | None
    revisions: tuple[MaterialRequestLineRevision, ...]

    @property
    def edited(self) -> bool:
        return bool(self.revisions)


def version(of: MaterialRequest) -> int:
    """What the page sends back as ``seen_version`` with a decision or an edit."""
    return revisions.version(of.pk)


def line_history(ctx: AccessContext, of: MaterialRequest) -> list[LineHistory]:
    """Every line of the request (removed ones included: they are rejected or closed,
    never deleted) against the copy of its latest submit, with the edits made since.

    Empty for who may not view the request. A request submitted before copies existed has
    one only once a reviewer edits it (``backfilled``).
    """
    if not ctx.can(VIEW, of):
        return []
    baseline = (
        MaterialRequestBaseline.objects.visible_to(ctx, VIEW)
        .filter(request=of)
        .order_by("-sequence")
        .first()
    )
    copies: dict[UUID, MaterialRequestBaselineLine] = {}
    edits: list[MaterialRequestLineRevision] = []
    if baseline is not None:
        copies = {copy.line_id: copy for copy in baseline.lines.select_related("material", "unit")}
        edits = list(
            MaterialRequestLineRevision.objects.visible_to(ctx, VIEW)
            .filter(baseline=baseline)
            .select_related("editor")
            .order_by("created_at", "id")
        )
    rows = (
        MaterialRequestLine.objects.visible_to(ctx, VIEW)
        .filter(request=of)
        .select_related("material", "unit")
        .order_by("line_no")
    )
    return [
        LineHistory(
            line=row,
            submitted=copies.get(row.pk),
            revisions=tuple(
                edit for edit in edits if row.pk in {edit.line_id, edit.replacement_line_id}
            ),
        )
        for row in rows
    ]


# --- the project's materials: what is still outstanding for the worksite (M6.5) ----------


@dataclass(frozen=True)
class ProjectMaterial:
    """One material (or free-text description) over the open lines of a project."""

    description: str
    reference: str
    unit_code: str
    requests: int
    approved: Decimal
    received: Decimal
    pending: Decimal
    delivered: Decimal
    awaiting_handover: Decimal
    outstanding: Decimal


def project_for_overview(ctx: AccessContext, project_id: UUID) -> Project:
    found = Project.objects.visible_to(ctx, VIEW).filter(pk=project_id).first()
    if found is None:
        raise NotInScope(VIEW)
    return found


def _project_material(entries: list[ledger.LineLedger]) -> ProjectMaterial:
    first = entries[0].line
    material = first.material
    return ProjectMaterial(
        description=material.name if material else first.description,
        reference=material.reference if material else "",
        unit_code=first.unit.code,
        requests=len({entry.line.request_id for entry in entries}),
        approved=sum((entry.approved for entry in entries), ledger.ZERO),
        received=sum((entry.flows.received for entry in entries), ledger.ZERO),
        pending=sum((entry.flows.pending for entry in entries), ledger.ZERO),
        delivered=sum((entry.flows.delivered for entry in entries), ledger.ZERO),
        awaiting_handover=sum((entry.awaiting_handover for entry in entries), ledger.ZERO),
        outstanding=sum((entry.outstanding for entry in entries), ledger.ZERO),
    )


MATERIAL_ORDER = ("seek_label", "seek_group", "unit_id")
"""A material of the project: its name (or a free text), the group, the unit."""


def _by_material(rows: QuerySet[MaterialRequestLine]) -> QuerySet[MaterialRequestLine]:
    """``rows`` with the fields of ``MATERIAL_ORDER``: a catalog material groups by its id, a
    free text by its words whatever their case; both with the unit."""
    return rows.annotate(
        seek_label=Lower(Coalesce("material__name", "free_text")),
        seek_group=Case(
            When(
                material__isnull=False, then=Concat(Value("m:"), Cast("material_id", CharField()))
            ),
            default=Concat(Value("t:"), Lower("free_text")),
            output_field=CharField(),
        ),
    )


class ProjectMaterials:
    """Per material, the open lines of the requests the user may see for this project, a
    page at a time (M6.5, M7d.4).

    Alphabetical by material. Only open lines count (approved requests, live lines), so a
    closed or cancelled request and a line closed short no longer weigh on the worksite.
    ``q`` looks at the description and the reference (in SQL); ``outstanding_only`` drops the
    materials with nothing left to arrive or hand over, as their ledgers are worked out.
    ``seek`` reads the next materials from the database, ``window`` works out the ledgers of
    a stretch of them only.
    """

    def __init__(
        self, ctx: AccessContext, of: Project, *, q: str = "", outstanding_only: bool = False
    ) -> None:
        lines = MaterialRequestLine.objects.visible_to(ctx, VIEW).filter(
            ledger.open_q(), request__project=of
        )
        needle = q.strip()
        if needle:
            lines = lines.filter(
                Q(material__name__icontains=needle)
                | Q(material__reference__icontains=needle)
                | Q(material__isnull=True, free_text__icontains=needle)
            )
        self._lines = _by_material(lines)
        self._outstanding_only = outstanding_only
        self._group: dict[str, tuple[str, UUID]] = {}

    def seek(self, cursor: str, limit: int) -> list[str]:
        """The keys of the next materials: the first line id of each (a cursor finds the
        material again from any of its lines)."""
        rows = seek_after(
            self._lines, _by_material(MaterialRequestLine.objects.all()), MATERIAL_ORDER, cursor
        )
        found = rows.values(*MATERIAL_ORDER).annotate(seek_key=Min(Cast("pk", CharField())))
        keys = []
        for row in found[:limit]:
            self._group[row["seek_key"]] = (row["seek_group"], row["unit_id"])
            keys.append(row["seek_key"])
        return keys

    def window(self, chunk: Sequence[str]) -> list[tuple[int, ProjectMaterial]]:
        wanted = {self._group[key] for key in chunk}
        lines = (
            self._lines.filter(
                Q(seek_group__in={group for group, _ in wanted}),
                unit_id__in={unit for _, unit in wanted},
            )
            .select_related("request", "material", "unit")
            .order_by("request__number", "line_no")
        )
        grouped: dict[tuple[str, UUID], list[ledger.LineLedger]] = {}
        for entry in ledger.line_ledgers(lines):
            pair = (getattr(entry.line, "seek_group"), entry.line.unit_id)  # noqa: B009
            if pair in wanted:
                grouped.setdefault(pair, []).append(entry)
        items = {pair: _project_material(entries) for pair, entries in grouped.items()}
        return [
            (index, items[self._group[key]])
            for index, key in enumerate(chunk)
            if self._group[key] in items and self._keeps(items[self._group[key]])
        ]

    def _keeps(self, item: ProjectMaterial) -> bool:
        return not self._outstanding_only or item.outstanding > 0
