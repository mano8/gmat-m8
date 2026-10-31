"""Reads for the organization screens (plan §3.4, §3.6).

Branch choices come from ``visible_to``, so a form never offers a branch outside the
user's scope for the capability it serves. Companies and memberships are installation-wide
data that only ``org.manage`` (group administrators, S2) reads here.
"""

from uuid import UUID

from django.db.models import QuerySet

from apps.access.context import AccessContext
from apps.access.exceptions import NotInScope
from apps.access.roles import Capability
from apps.accounts.models import User
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


def memberships(ctx: AccessContext, of: User) -> QuerySet[Membership]:
    """Every membership of ``of``, open ones first, for administrators."""
    if not ctx.has(Capability.ORG_MANAGE):
        return Membership.objects.none()
    return (
        Membership.objects.filter(user=of)
        .select_related("branch__company", "company", "revoked_by")
        .order_by("revoked_at", "-valid_from")
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
