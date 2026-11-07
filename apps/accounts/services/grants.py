"""What an invitation grants: parsed, validated when it is created, created on acceptance."""

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from django.core.exceptions import ValidationError
from django.utils.dateparse import parse_datetime
from django.utils.translation import gettext_lazy as _

from apps.access.roles import PRIVILEGED_ROLES
from apps.accounts.models import User
from apps.audit.recorder import AuditIdentity, record
from apps.core import events
from apps.core.db import db_now
from apps.organizations.grants import check_membership_target, elevate_privilege
from apps.organizations.models import Membership
from apps.projects.models import Project, ProjectAssignment


@dataclass(frozen=True)
class MembershipGrant:
    """A membership an invitation creates; it starts when the invitation is accepted."""

    role: str
    branch_id: UUID | None = None
    company_id: UUID | None = None
    valid_until: datetime | None = None


@dataclass(frozen=True)
class AssignmentGrant:
    """A project assignment an invitation creates (site managers and partners)."""

    project_id: UUID
    kind: str
    valid_until: datetime | None = None


def _grants_json(
    memberships: Sequence[MembershipGrant], assignments: Sequence[AssignmentGrant]
) -> dict[str, list[dict[str, Any]]]:
    def plain(item: MembershipGrant | AssignmentGrant) -> dict[str, Any]:
        return {
            key: (str(value) if isinstance(value, UUID) else value)
            if not isinstance(value, datetime)
            else value.isoformat()
            for key, value in asdict(item).items()
        }

    return {
        "memberships": [plain(item) for item in memberships],
        "assignments": [plain(item) for item in assignments],
    }


def _uuid(value: Any) -> UUID | None:
    return UUID(str(value)) if value else None


def _when(value: Any) -> datetime | None:
    return parse_datetime(value) if value else None


def parse_grants(grants: Mapping[str, Any]) -> tuple[list[MembershipGrant], list[AssignmentGrant]]:
    """The grants stored on an invitation."""
    memberships = [
        MembershipGrant(
            role=str(item["role"]),
            branch_id=_uuid(item.get("branch_id")),
            company_id=_uuid(item.get("company_id")),
            valid_until=_when(item.get("valid_until")),
        )
        for item in grants.get("memberships", [])
    ]
    assignments = [
        AssignmentGrant(
            project_id=UUID(str(item["project_id"])),
            kind=str(item["kind"]),
            valid_until=_when(item.get("valid_until")),
        )
        for item in grants.get("assignments", [])
    ]
    return memberships, assignments


def _unsaved_membership(grant: MembershipGrant, user: User | None, start: datetime) -> Membership:
    membership = Membership(
        role=grant.role,
        branch_id=grant.branch_id,
        company_id=grant.company_id,
        valid_from=start,
        valid_until=grant.valid_until,
    )
    if user is not None:
        membership.user = user
    return membership


def _active_project(project_id: UUID) -> Project:
    project = Project.objects.select_related("branch__company").filter(pk=project_id).first()
    if (
        project is None
        or project.status != Project.Status.ACTIVE
        or not (project.branch.is_active and project.branch.company.is_active)
    ):
        raise ValidationError(
            {"project": ValidationError(_("The project is not active."), code="inactive")}
        )
    return project


def _validate_grants(
    memberships: Sequence[MembershipGrant],
    assignments: Sequence[AssignmentGrant],
    employer_company_id: UUID | None,
) -> None:
    """Every grant has its role's shape and an active target (plan §5, step 2)."""
    if not memberships:
        raise ValidationError(_("An invitation grants at least one role."), code="no_grant")
    start = db_now()
    for grant in memberships:
        membership = _unsaved_membership(grant, None, start)
        membership.full_clean(exclude=["user", "revoked_by"])
        check_membership_target(membership, employer_company_id)
    for item in assignments:
        if item.kind not in ProjectAssignment.Kind.values:
            raise ValidationError({"kind": ValidationError(_("Unknown kind."), code="invalid")})
        _active_project(item.project_id)


def _grant_on_acceptance(
    identity: AuditIdentity,
    user: User,
    memberships: Sequence[MembershipGrant],
    assignments: Sequence[AssignmentGrant],
) -> None:
    start = db_now()
    for grant in memberships:
        membership = _unsaved_membership(grant, user, start)
        membership.full_clean()
        check_membership_target(membership, user.employer_company_id)
        membership.save()
        if membership.role in PRIVILEGED_ROLES:
            elevate_privilege(user.pk)
        record(
            identity,
            "membership.granted",
            obj=membership,
            branch_id=membership.branch_id,
            data={
                "user_id": user.pk,
                "role": membership.role,
                "branch_id": membership.branch_id,
                "company_id": membership.company_id,
                "valid_from": membership.valid_from,
                "valid_until": membership.valid_until,
            },
        )
        events.publish("membership.granted", membership_id=membership.pk, user_id=user.pk)
    for item in assignments:
        project = _active_project(item.project_id)
        if ProjectAssignment.objects.filter(
            project=project, user=user, kind=item.kind, revoked_at__isnull=True
        ).exists():
            continue  # already shared with this account
        assignment = ProjectAssignment(
            project=project,
            user=user,
            kind=item.kind,
            valid_from=start,
            valid_until=item.valid_until,
        )
        assignment.full_clean()
        assignment.save()
        record(
            identity,
            "project_assignment.granted",
            obj=assignment,
            branch_id=project.branch_id,
            data={"project_id": project.pk, "user_id": user.pk, "kind": item.kind},
        )
        events.publish("project_assignment.granted", assignment_id=assignment.pk, user_id=user.pk)
