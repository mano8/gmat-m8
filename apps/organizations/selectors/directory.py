"""The choices of the people screens, a person's memberships and the people per branch.

Branch choices come from ``visible_to``, so a form never offers a branch outside the
user's scope for the capability it serves. Companies and memberships are installation-wide
data that only ``org.manage`` (group administrators, S2) reads here.
"""

from collections.abc import Collection
from uuid import UUID

from django.db.models import Count, IntegerField, OuterRef, QuerySet, Subquery
from django.db.models.functions import Coalesce

from apps.access.context import AccessContext
from apps.access.exceptions import NotInScope
from apps.access.policy import valid_now
from apps.access.roles import Capability
from apps.accounts.models import User
from apps.core.listing import OPEN_FIRST, by_revocation
from apps.organizations.models import Branch, Company, Membership


def branches(ctx: AccessContext, capability: str) -> QuerySet[Branch]:
    """Active branches of active companies within ``capability``'s scope."""
    return (
        Branch.objects.visible_to(ctx, capability)
        .filter(is_active=True, company__is_active=True)
        .select_related("company")
        .order_by("company__code", "code")
    )


def companies(ctx: AccessContext) -> QuerySet[Company]:
    """Active companies, for administrators (employer, partner memberships)."""
    if not ctx.has(Capability.ORG_MANAGE):
        return Company.objects.none()
    return Company.objects.filter(is_active=True).order_by("code")


def memberships(ctx: AccessContext, of: User, *, state: str = "") -> QuerySet[Membership]:
    """Every membership of ``of``, open ones first, for administrators; ``state`` is
    ``listing.ACTIVE`` or ``REVOKED`` to keep one kind."""
    if not ctx.has(Capability.ORG_MANAGE):
        return Membership.objects.none()
    return (
        by_revocation(Membership.objects.filter(user=of), state)
        .select_related("branch__company", "company", "revoked_by")
        .order_by(OPEN_FIRST, "-valid_from")
    )


def membership(ctx: AccessContext, membership_id: UUID) -> Membership:
    found = (
        Membership.objects.select_related("user").filter(pk=membership_id).first()
        if ctx.has(Capability.ORG_MANAGE)
        else None
    )
    if found is None:
        raise NotInScope(Capability.ORG_MANAGE)
    return found


def people_per_branch(ctx: AccessContext) -> list[tuple[Branch, int]]:
    """For administrators: each active branch with the people holding a valid role on it
    (an active account with a membership valid now, S8), by company and code. Group
    administrators and partners hold company-wide roles, so no branch counts them."""
    if not ctx.has(Capability.ORG_MANAGE):
        return []
    people = (
        Membership.objects.filter(
            valid_now(),
            branch=OuterRef("pk"),
            user__is_active=True,
            user__offboarded_at__isnull=True,
        )
        .order_by()
        .values("branch")
        .annotate(total=Count("user", distinct=True))
        .values("total")
    )
    rows = branches(ctx, Capability.ORG_MANAGE).annotate(
        people=Coalesce(Subquery(people, output_field=IntegerField()), 0)
    )
    return [(branch, branch.people) for branch in rows]


def branch_labels(ctx: AccessContext, ids: Collection[UUID]) -> dict[UUID, str]:
    """ "<company code> · <branch name>" of these branches, for administrators (inactive ones
    too: a chart of past weeks may count them)."""
    if not ctx.has(Capability.ORG_MANAGE):
        return {}
    rows = Branch.objects.visible_to(ctx, Capability.ORG_MANAGE).filter(pk__in=list(ids))
    return {
        pk: f"{code} · {name}" for pk, code, name in rows.values_list("pk", "company__code", "name")
    }
