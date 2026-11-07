"""Reads for the reviewer: the inbox, who is assigned, what the review page offers (M4.4, M7b.2)."""

from collections.abc import Sequence
from datetime import date, timedelta
from uuid import UUID

from django.db.models import Case, Count, Exists, F, IntegerField, OuterRef, QuerySet, Value, When
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.access.context import AccessContext
from apps.access.roles import SELF_DECIDING_ROLES, Capability
from apps.access.scopes import object_in_scope
from apps.accounts.models import User
from apps.catalog import selectors as catalog_selectors
from apps.catalog.models import Material
from apps.material_requests import ledger, reviewers
from apps.material_requests.models import MaterialRequest, MaterialRequestLine
from apps.material_requests.selectors.requester import Status, can_edit
from apps.projects.models import Project

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
# How far back the dashboard looks for closed requests that need attention.
CLOSED_WINDOW_DAYS = 30
REVIEW, SOURCE, CANCEL_ANY = (
    Capability.REQUEST_REVIEW,
    Capability.REQUEST_SOURCE,
    Capability.REQUEST_CANCEL_ANY,
)
EDIT = Capability.REQUEST_EDIT


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
    """Deciding, returning and reducing, of one's own request only where the procurement
    branch allows it or a self-deciding role's grant covers the request (Q33; the services
    check again)."""
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
