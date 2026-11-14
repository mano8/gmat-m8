"""Reads of the organization's structure screens (M10.2): companies, joint ventures, branches.

Companies and joint-venture members are installation-wide data that only ``org.manage``
(group administrators, S2) reads; branches come through ``visible_to``. Inactive companies
and branches are included: an administrator reactivates them from these screens.
"""

from uuid import UUID

from django.db.models import Count, IntegerField, OuterRef, Q, QuerySet, Subquery
from django.db.models.functions import Coalesce
from django.utils.translation import gettext_lazy as _

from apps.access.context import AccessContext
from apps.access.exceptions import NotInScope
from apps.access.policy import valid_now
from apps.access.roles import Capability
from apps.locations.models import Location
from apps.organizations.models import Branch, Company, JointVentureMember, Membership
from apps.projects.models import Project

ORG = Capability.ORG_MANAGE
DEFAULT_PER_PAGE = 25
SECTION_PER_PAGE = 10
STATE_ACTIVE, STATE_INACTIVE = "active", "inactive"

COMPANY_SORTS = {"code": ("code",), "name": ("legal_name",)}
COMPANY_SORT_LABELS = {"code": _("Code"), "name": _("Legal name")}
BRANCH_SORTS = {"code": ("company__code", "code"), "name": ("name",), "city": ("city",)}
BRANCH_SORT_LABELS = {"code": _("Code"), "name": _("Name"), "city": _("City")}


def _by_state[T: (Company, Branch, Location)](rows: QuerySet[T], state: str) -> QuerySet[T]:
    if state == STATE_ACTIVE:
        return rows.filter(is_active=True)
    if state == STATE_INACTIVE:
        return rows.filter(is_active=False)
    return rows


def _companies(ctx: AccessContext) -> QuerySet[Company]:
    return Company.objects.all() if ctx.has(ORG) else Company.objects.none()


def company_list(
    ctx: AccessContext, *, q: str = "", state: str = "", kind: str = ""
) -> QuerySet[Company]:
    """Every company, filtered by text (code, legal name, tax id), state and kind, with the
    number of its branches."""
    branches = (
        Branch.objects.filter(company=OuterRef("pk"))
        .order_by()
        .values("company")
        .annotate(total=Count("pk"))
        .values("total")
    )
    rows = _by_state(_companies(ctx), state)
    if kind:
        rows = rows.filter(kind=kind)
    if q:
        rows = rows.filter(
            Q(code__icontains=q) | Q(legal_name__icontains=q) | Q(tax_id__icontains=q)
        )
    return rows.annotate(
        branch_count=Coalesce(Subquery(branches, output_field=IntegerField()), 0)
    ).order_by("code")


def company(ctx: AccessContext, company_id: UUID) -> Company:
    """A company, inactive ones included; ``NotInScope`` (404) when unknown or not readable."""
    found = _companies(ctx).filter(pk=company_id).first()
    if found is None:
        raise NotInScope(ORG)
    return found


def company_choices(ctx: AccessContext) -> QuerySet[Company]:
    """Every company, for a filter (inactive ones too: their branches are listed)."""
    return _companies(ctx).order_by("code")


def company_branches(ctx: AccessContext, of: Company, *, state: str = "") -> QuerySet[Branch]:
    """The branches of a company, by code (the company page's section)."""
    rows = Branch.objects.visible_to(ctx, ORG).filter(company=of)
    return _by_state(rows, state).select_related("company", "managed_by__company").order_by("code")


def people_count(ctx: AccessContext, of: Company) -> int:
    """People with a valid role on a branch of the company or on the company itself (a
    partner's), active accounts only (S8)."""
    if not ctx.has(ORG):
        return 0
    return (
        Membership.objects.filter(
            valid_now(), user__is_active=True, user__offboarded_at__isnull=True
        )
        .filter(Q(branch__company=of) | Q(company=of))
        .values("user")
        .distinct()
        .count()
    )


# --- joint ventures -----------------------------------------------------------------------


def venture_members(ctx: AccessContext, venture: Company) -> QuerySet[JointVentureMember]:
    """The members of a joint venture, by the member's code."""
    if not ctx.has(ORG):
        return JointVentureMember.objects.none()
    return (
        JointVentureMember.objects.filter(venture=venture)
        .select_related("member")
        .order_by("member__code")
    )


def venture_member(ctx: AccessContext, member_id: UUID) -> JointVentureMember:
    found = (
        JointVentureMember.objects.select_related("venture", "member").filter(pk=member_id).first()
        if ctx.has(ORG)
        else None
    )
    if found is None:
        raise NotInScope(ORG)
    return found


def venture_candidates(ctx: AccessContext, venture: Company) -> QuerySet[Company]:
    """Active companies that could join the venture: not itself, not a member already."""
    members = JointVentureMember.objects.filter(venture=venture).values("member")
    return (
        _companies(ctx)
        .filter(is_active=True)
        .exclude(pk=venture.pk)
        .exclude(pk__in=members)
        .order_by("code")
    )


# --- branches -------------------------------------------------------------------------------


def branch_list(
    ctx: AccessContext, *, q: str = "", company_id: UUID | None = None, state: str = ""
) -> QuerySet[Branch]:
    """Every branch in scope, filtered by text (code, name, city, company code), company
    and state."""
    rows = _by_state(Branch.objects.visible_to(ctx, ORG), state)
    if company_id is not None:
        rows = rows.filter(company_id=company_id)
    if q:
        rows = rows.filter(
            Q(code__icontains=q)
            | Q(name__icontains=q)
            | Q(city__icontains=q)
            | Q(company__code__icontains=q)
        )
    return rows.select_related("company", "managed_by__company").order_by("company__code", "code")


def branch(ctx: AccessContext, branch_id: UUID) -> Branch:
    """A branch, inactive ones included; ``NotInScope`` (404) when unknown or out of scope."""
    found = (
        Branch.objects.visible_to(ctx, ORG)
        .select_related("company", "managed_by__company", "default_warehouse")
        .filter(pk=branch_id)
        .first()
    )
    if found is None:
        raise NotInScope(ORG)
    return found


def managed_branches(ctx: AccessContext, of: Branch, *, state: str = "") -> QuerySet[Branch]:
    """The branches ``of`` manages, by company and code."""
    rows = Branch.objects.visible_to(ctx, ORG).filter(managed_by=of)
    return _by_state(rows, state).select_related("company").order_by("company__code", "code")


def manager_candidates(ctx: AccessContext, of: Branch | None = None) -> QuerySet[Branch]:
    """The branches that may manage ``of`` (or a new branch): one level only, so a branch
    that no other branch manages, never ``of`` itself; none when ``of`` manages others.
    Inactive ones are offered too, as the admin did; their label says so."""
    rows = Branch.objects.visible_to(ctx, ORG).filter(managed_by__isnull=True)
    if of is not None:
        if Branch.objects.filter(managed_by=of).exists():
            return Branch.objects.none()
        rows = rows.exclude(pk=of.pk)
    return rows.select_related("company").order_by("company__code", "code")


def warehouses(ctx: AccessContext, of: Branch, *, state: str = "") -> QuerySet[Location]:
    """The warehouses of a branch, by name (the branch page's section)."""
    rows = Location.objects.visible_to(ctx, ORG).filter(branch=of, kind=Location.Kind.WAREHOUSE)
    return _by_state(rows, state).order_by("name")


def default_warehouse_choices(ctx: AccessContext, of: Branch) -> QuerySet[Location]:
    """The active warehouses of a branch: what its default may be."""
    return warehouses(ctx, of, state=STATE_ACTIVE)


def project_counts(ctx: AccessContext, of: Branch) -> dict[str, int]:
    """How many projects the branch executes (``branch``) and buys for (``procurement``)."""
    if not ctx.has(ORG):
        return {"executes": 0, "buys": 0}
    counts = Project.objects.filter(Q(branch=of) | Q(procurement_branch=of)).aggregate(
        executes=Count("pk", filter=Q(branch=of)),
        buys=Count("pk", filter=Q(procurement_branch=of)),
    )
    return {"executes": counts["executes"], "buys": counts["buys"]}
