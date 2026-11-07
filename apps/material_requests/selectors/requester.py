"""Reads for the requester's screens: projects, their requests and lines, what the page offers."""

from collections.abc import Sequence
from uuid import UUID

from django.db.models import F, Max, Q, QuerySet
from django.utils.translation import gettext_lazy as _

from apps.access.context import AccessContext
from apps.access.exceptions import NotInScope
from apps.access.roles import Capability
from apps.catalog import selectors as catalog_selectors
from apps.catalog.models import Material
from apps.core.listing import ListScope
from apps.material_requests import ledger
from apps.material_requests.models import MaterialRequest, MaterialRequestLine
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
