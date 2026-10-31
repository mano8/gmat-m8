"""Reads for the project screens (plan §3.5, §3.6): every row through ``visible_to``.

A project the user cannot see is ``NotInScope`` (404). Assignments name other people, so
only those who manage the project (``project.manage`` on it) see them; a partner or a site
manager sees the project, never who else it is shared with.
"""

from uuid import UUID

from django.db.models import Q, QuerySet

from apps.access.context import AccessContext
from apps.access.exceptions import NotInScope
from apps.access.policy import valid_now
from apps.access.roles import ROLE_ASSIGNMENT_KIND, Capability, Role
from apps.accounts.models import User
from apps.organizations.models import Membership
from apps.projects.models import Project, ProjectAssignment

PAGE_SIZE = 25


def projects(ctx: AccessContext, *, q: str = "", status: str = "") -> QuerySet[Project]:
    """The projects ``ctx`` may view, filtered by text and status."""
    rows = Project.objects.visible_to(ctx, Capability.PROJECT_VIEW).select_related(
        "branch__company", "procurement_branch__company"
    )
    if q:
        rows = rows.filter(
            Q(code__icontains=q) | Q(name__icontains=q) | Q(client_name__icontains=q)
        )
    if status:
        rows = rows.filter(status=status)
    return rows.order_by("code")


def project(ctx: AccessContext, project_id: UUID) -> Project:
    found = projects(ctx).filter(pk=project_id).first()
    if found is None:
        raise NotInScope(Capability.PROJECT_VIEW)
    return found


def assignments(ctx: AccessContext, of: Project) -> QuerySet[ProjectAssignment]:
    """The project's assignments, newest first, for those who manage it; otherwise none."""
    if not ctx.can(Capability.PROJECT_MANAGE, of):
        return ProjectAssignment.objects.none()
    return (
        ProjectAssignment.objects.filter(project=of)
        .select_related("user", "revoked_by")
        .order_by("revoked_at", "-valid_from")
    )


def assignment(ctx: AccessContext, assignment_id: UUID) -> ProjectAssignment:
    """An assignment of a project ``ctx`` can view (404 otherwise)."""
    found = (
        ProjectAssignment.objects.select_related("project", "user").filter(pk=assignment_id).first()
    )
    if found is None or not ctx.can(Capability.PROJECT_VIEW, found.project):
        raise NotInScope(Capability.PROJECT_VIEW)
    return found


_KIND_ROLE = {kind: role for role, kind in ROLE_ASSIGNMENT_KIND.items()}


def assignment_candidates(ctx: AccessContext, kind: str | None = None) -> QuerySet[User]:
    """Accounts a project can be shared with (S4), for the assignment form's choices.

    Site managers whose home branch is within ``ctx``'s ``project.manage`` scope, and
    partners of active companies; both active, with a valid membership of that role.
    ``kind`` narrows to one ``ProjectAssignment.Kind``.
    """
    scope = ctx.scope_for(Capability.PROJECT_MANAGE)
    if not scope:
        return User.objects.none()
    kinds = [kind] if kind else list(_KIND_ROLE)
    condition = Q(pk__in=[])
    for item in kinds:
        role = _KIND_ROLE[item]
        if role is Role.SITE_MANAGER:
            condition |= Q(role=role) & scope.branch_q("branch")
        else:  # partners: any active company may employ them
            condition |= Q(role=role, company__is_active=True)
    user_ids = Membership.objects.filter(valid_now(), condition).values("user_id")
    return User.objects.filter(
        pk__in=user_ids, is_active=True, offboarded_at__isnull=True
    ).order_by("email")


def manageable(ctx: AccessContext, of: Project) -> bool:
    return ctx.can(Capability.PROJECT_MANAGE, of)
