"""Reviewer assignment: who may be assigned a request, and who is (M7b.2, ADR-0014 §4).

An assignee holds ``request.review`` through a membership of a self-deciding role
(``ASSIGNABLE_ROLES``: group administrators, material managers, engineers) whose scope
covers the request's procurement branch. A role held through another membership counts
for nothing (S6), and the memberships are evaluated at the database clock (S8, S9), so a
revoked or expired membership, a deactivated account or an inactive branch grants nothing.

For these roles ``request.review`` reaches whole branches, so "covers the procurement
branch" is the request's own scope rule (S5) without the request row. That lets a draft be
judged against the branch its submit will take.

The assignment never restricts who decides (Q31). A stored assignee who no longer
qualifies reads as unassigned (``assignee``); nothing is rewritten.
"""

from uuid import UUID

from django.db.models import Q, QuerySet

from apps.access.context import AccessContext
from apps.access.policy import valid_now
from apps.access.roles import SELF_DECIDING_ROLES, Capability, Role
from apps.access.scopes import Scope
from apps.accounts.models import User
from apps.material_requests.models import MaterialRequest
from apps.organizations.models import Branch, Membership

ASSIGNABLE_ROLES = SELF_DECIDING_ROLES


def assignable_scope(ctx: AccessContext) -> Scope:
    """The grants through which ``ctx``'s user may be assigned requests (S6)."""
    return ctx.scope_for(Capability.REQUEST_REVIEW).of_roles(ASSIGNABLE_ROLES)


def covers(user: User, branch_id: UUID) -> bool:
    """``user`` may be assigned the requests that ``branch_id`` buys for."""
    scope = assignable_scope(AccessContext.for_user(user))
    return bool(scope) and Branch.objects.filter(scope.branch_q(), pk=branch_id).exists()


def target_branch(request: MaterialRequest) -> UUID:
    """The procurement branch the assignee must cover: a draft's comes from its project
    when it is submitted, so it is read there."""
    if request.status == MaterialRequest.Status.DRAFT:
        return request.project.procurement_branch_id
    return request.procurement_branch_id


def candidates(branch_id: UUID) -> list[User]:
    """The users who may be assigned a request of ``branch_id``, by name.

    Pre-selected on the valid memberships that could reach the branch (a group
    administrator anywhere, a branch role on it or on the branch managing it, S1), then
    checked one by one through ``covers``.
    """
    managing = Branch.objects.filter(pk=branch_id).values_list("managed_by_id", flat=True)
    near = Q(branch_id=branch_id) | Q(branch_id__in=managing)
    memberships = Membership.objects.filter(valid_now(), role__in=ASSIGNABLE_ROLES).filter(
        Q(role=Role.GROUP_ADMIN) | near
    )
    users: QuerySet[User] = User.objects.filter(
        pk__in=memberships.values("user_id"), is_active=True, offboarded_at__isnull=True
    ).order_by("full_name", "email")
    return [user for user in users if covers(user, branch_id)]


def assignee(request: MaterialRequest) -> User | None:
    """The request's assigned reviewer while they still qualify, else ``None``."""
    user = request.assigned_reviewer
    if user is None or not covers(user, request.procurement_branch_id):
        return None
    return user
