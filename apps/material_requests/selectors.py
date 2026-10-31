"""Reads for the request screens (plan §3.5, §4.2, M4.3): every row through ``visible_to``.

A request or line the user cannot see is ``NotInScope`` (404). The screens of the
requester show the user's own requests; ``request.view`` decides what else a reviewer may
open. Nothing here returns a price: requests carry none, and the catalog has none.

The ``can_*`` predicates only decide what the page offers (the services check again, in
their transaction): ownership, the state and, for a withdrawal, that no line is decided.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from django.db.models import Case, Count, F, IntegerField, Max, Q, QuerySet, Value, When
from django.utils.translation import gettext_lazy as _

from apps.access.context import AccessContext
from apps.access.exceptions import NotInScope
from apps.access.roles import Capability
from apps.audit.models import AuditEvent
from apps.catalog import selectors as catalog_selectors
from apps.catalog.models import Material
from apps.material_requests import ledger
from apps.material_requests.models import MaterialRequest, MaterialRequestLine
from apps.projects.models import Project

PAGE_SIZE = 10
PROJECT_PAGE_SIZE = 12
RECENT_MATERIALS = 8
# How many of the user's latest lines are looked at for the recent materials.
RECENT_WINDOW = 60

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


def my_requests(ctx: AccessContext, *, status: str = "") -> QuerySet[MaterialRequest]:
    """The user's own requests, newest first, optionally in one status."""
    rows = (
        MaterialRequest.objects.visible_to(ctx, VIEW)
        .filter(requested_by_id=ctx.user_id)
        .select_related("project")
    )
    if status:
        rows = rows.filter(status=status)
    return rows.order_by("-created_at")


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
) -> QuerySet[MaterialRequest]:
    """Requests in the user's review scope in one status, the most pressing first."""
    rows = (
        MaterialRequest.objects.visible_to(ctx, REVIEW)
        .filter(status=status if status in INBOX_STATUSES else Status.SUBMITTED)
        .select_related("project", "requested_by")
        .annotate(line_count=Count("lines"))
    )
    if priority:
        rows = rows.filter(priority=priority)
    if project_id is not None:
        rows = rows.filter(project_id=project_id)
    if needed_before is not None:
        rows = rows.filter(needed_by__lte=needed_before)
    return rows.order_by(PRIORITY_RANK, F("needed_by").asc(nulls_last=True), "submitted_at")


def inbox_projects(ctx: AccessContext) -> QuerySet[Project]:
    """The projects of the inbox filter: the ones whose requests the user reviews."""
    return Project.objects.visible_to(ctx, Capability.PROJECT_VIEW).order_by("code")


def link_candidates(ctx: AccessContext, of: MaterialRequestLine, query: str) -> list[Material]:
    """Active catalog materials matching ``query`` in the unit of a non-catalog line.

    Only a material in the line's own unit can be linked (no conversions, Q15).
    """
    return [m for m in catalog_selectors.search(ctx, query) if m.unit_id == of.unit_id]


def can_review(ctx: AccessContext, of: MaterialRequest) -> bool:
    """Decisions, return for changes and reductions (the services check again)."""
    return ctx.can(REVIEW, of)


def can_decide(ctx: AccessContext, of: MaterialRequest) -> bool:
    """Review that is not an approval of one's own request (unless the branch allows it)."""
    return can_review(ctx, of) and (
        of.requested_by_id != ctx.user_id or of.procurement_branch.allow_self_approval
    )


def can_source(ctx: AccessContext, of: MaterialRequest) -> bool:
    """Sourcing, schedule, resolution and closing (the services check again)."""
    return ctx.can(SOURCE, of)


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


def _detail(action: str, data: dict[str, Any]) -> str:
    """The reason or note of an event, or what it changed; never the raw payload."""
    changes = data.get("changes") or {}
    if action == "request_line.decided":
        decision = (changes.get("decision") or {}).get("to", "")
        quantity = (changes.get("approved_qty") or {}).get("to")
        note = (changes.get("decision_note") or {}).get("to", "")
        parts = [
            str(MaterialRequestLine.Decision(decision).label) if decision else "",
            _quantity(quantity) if quantity is not None else "",
            note,
        ]
        return " · ".join(part for part in parts if part)
    if action == "request_line.reduced":
        change = changes.get("approved_qty") or {}
        arrow = f"{_quantity(change['from'])} → {_quantity(change['to'])}" if change else ""
        return " · ".join(part for part in (arrow, data.get("reason", "")) if part)
    if action == "request_line.sourcing_set":
        return str((changes.get("sourcing") or {}).get("to", ""))
    if action == "request.rescheduled":
        return " · ".join(f"{name}: {change.get('to') or '—'}" for name, change in changes.items())
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
