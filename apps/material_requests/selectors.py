"""Reads for the request screens (plan §3.5, §4.2, M4.3): every row through ``visible_to``.

A request or line the user cannot see is ``NotInScope`` (404). The screens of the
requester show the user's own requests; ``request.view`` decides what else a reviewer may
open. Nothing here returns a price: requests carry none, and the catalog has none.

The ``can_*`` predicates only decide what the page offers (the services check again, in
their transaction): ownership, the state and, for a withdrawal, that no line is decided.
"""

from collections.abc import Sequence
from uuid import UUID

from django.db.models import F, Max, Q, QuerySet

from apps.access.context import AccessContext
from apps.access.exceptions import NotInScope
from apps.access.roles import Capability
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
        .select_related("request", "material", "unit")
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
