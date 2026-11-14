"""Reads of the warehouse and worksite screens (M10.3): every row through ``visible_to``.

Warehouses and worksites are changed by ``org.manage`` (group administrators), whose scope
reaches every branch and project (S2), so out-of-scope ids are 404. Inactive ones are included:
an administrator reactivates them from these screens. A branch's default warehouse is marked,
because the service refuses to deactivate it.
"""

from uuid import UUID

from django.db.models import Exists, OuterRef, Q, QuerySet
from django.utils.translation import gettext_lazy as _

from apps.access.context import AccessContext
from apps.access.exceptions import NotInScope
from apps.access.roles import Capability
from apps.locations.models import Location
from apps.organizations.models import Branch
from apps.organizations.selectors import branch_list, branches

ORG = Capability.ORG_MANAGE
DEFAULT_PER_PAGE = 25
STATE_ACTIVE, STATE_INACTIVE = "active", "inactive"

WAREHOUSE_SORTS = {"name": ("name",), "branch": ("branch__company__code", "branch__code", "name")}
WAREHOUSE_SORT_LABELS = {"name": _("Name"), "branch": _("Branch")}


def warehouses(
    ctx: AccessContext, *, q: str = "", branch_id: UUID | None = None, state: str = ""
) -> QuerySet[Location]:
    """Every warehouse in scope, filtered by text (name, address, branch code), branch and
    state, each with whether it is its branch's default (``is_default``)."""
    rows = Location.objects.visible_to(ctx, ORG).filter(kind=Location.Kind.WAREHOUSE)
    if state == STATE_ACTIVE:
        rows = rows.filter(is_active=True)
    elif state == STATE_INACTIVE:
        rows = rows.filter(is_active=False)
    if branch_id is not None:
        rows = rows.filter(branch_id=branch_id)
    if q:
        rows = rows.filter(
            Q(name__icontains=q)
            | Q(address__icontains=q)
            | Q(branch__code__icontains=q)
            | Q(branch__company__code__icontains=q)
        )
    default = Branch.objects.filter(default_warehouse_id=OuterRef("pk"))
    return rows.select_related("branch__company").annotate(is_default=Exists(default))


def warehouse(ctx: AccessContext, location_id: UUID) -> Location:
    """A warehouse, inactive ones included; ``NotInScope`` (404) when unknown, a worksite or
    out of scope."""
    found = warehouses(ctx).filter(pk=location_id).first()
    if found is None:
        raise NotInScope(ORG)
    return found


def worksite(ctx: AccessContext, location_id: UUID) -> Location:
    """A worksite, inactive ones included; ``NotInScope`` (404) when unknown, a warehouse or
    out of scope."""
    found = (
        Location.objects.visible_to(ctx, ORG)
        .filter(kind=Location.Kind.WORKSITE, pk=location_id)
        .select_related("project", "branch__company")
        .first()
    )
    if found is None:
        raise NotInScope(ORG)
    return found


def worksite_of(ctx: AccessContext, project_id: UUID) -> Location | None:
    """The worksite of a project, for who may change it (``org.manage``), else ``None``."""
    if not ctx.has(ORG):
        return None
    return (
        Location.objects.visible_to(ctx, ORG)
        .filter(kind=Location.Kind.WORKSITE, project_id=project_id)
        .first()
    )


def branch_filter_choices(ctx: AccessContext) -> QuerySet[Branch]:
    """Every branch in scope, inactive ones too: their warehouses are listed."""
    return branch_list(ctx)


def new_warehouse_branches(ctx: AccessContext) -> QuerySet[Branch]:
    """The branches a warehouse may be added to: the active ones of active companies."""
    return branches(ctx, ORG)
