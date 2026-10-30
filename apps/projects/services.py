"""Project and assignment services (plan §3.3, §3.6).

Projects are organization changes (``AccessState`` ``FOR UPDATE``: a project's branches
and status define scopes); assignments change one user's authority (that ``User`` row
``FOR NO KEY UPDATE``). Same write order as ``apps.organizations.services``; the
capability is ``project.manage``.
"""

from datetime import date, datetime
from typing import Any
from uuid import UUID

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.access.authorization import PROJECT_MANAGE, authorize_in_txn
from apps.access.generations import lock_access_state
from apps.access.principals import Principal
from apps.audit.recorder import record
from apps.core import events
from apps.core.changes import apply_changes
from apps.core.db import db_now
from apps.core.idempotency import execute_once
from apps.core.locking import LockMode, LockOrder, transactional
from apps.locations.models import Location
from apps.organizations.models import Branch
from apps.organizations.services import lock_for_user_change
from apps.projects.models import Project, ProjectAssignment

PROJECT_FIELDS = frozenset(
    {
        "name",
        "client_name",
        "address",
        "branch_id",
        "procurement_branch_id",
        "status",
        "start_date",
        "end_date",
    }
)


def _project_change(principal: Principal) -> None:
    locks = LockOrder()
    lock_access_state(locks, mode=LockMode.UPDATE)
    authorize_in_txn(principal, PROJECT_MANAGE, locks=locks)


def _active_branch(branch_id: UUID, field: str) -> Branch:
    """An active branch of an active company; new projects only go there (S9)."""
    branch = Branch.objects.select_related("company").get(pk=branch_id)
    if not (branch.is_active and branch.company.is_active):
        raise ValidationError(
            {field: ValidationError(_("The branch is inactive."), code="inactive")}
        )
    return branch


@transactional
def create_project(
    principal: Principal,
    *,
    code: str,
    name: str,
    branch_id: UUID,
    procurement_branch_id: UUID | None = None,
    client_name: str = "",
    address: str = "",
    start_date: date | None = None,
    end_date: date | None = None,
    idempotency_key: str | None = None,
) -> Project:
    """Create a project and its worksite location (plan §3.3).

    ``procurement_branch`` defaults to the branch that manages ``branch``, or ``branch``
    itself (Q2: the procurement branch buys for the project).
    """
    _project_change(principal)
    payload = {
        "code": code,
        "name": name,
        "branch_id": branch_id,
        "procurement_branch_id": procurement_branch_id,
        "client_name": client_name,
        "address": address,
        "start_date": start_date,
        "end_date": end_date,
    }

    def execute() -> Project:
        branch = _active_branch(branch_id, "branch")
        procurement = procurement_branch_id or branch.managed_by_id or branch.pk
        _active_branch(procurement, "procurement_branch")
        project = Project(
            code=code,
            name=name,
            branch=branch,
            procurement_branch_id=procurement,
            client_name=client_name,
            address=address,
            start_date=start_date,
            end_date=end_date,
        )
        project.full_clean()
        project.save()
        worksite = Location(
            kind=Location.Kind.WORKSITE,
            branch=branch,
            project=project,
            name=project.name,
            address=project.address,
        )
        worksite.full_clean()
        worksite.save()
        identity = principal.audit_identity
        record(
            identity,
            "project.created",
            obj=project,
            branch_id=branch.pk,
            data={
                "code": code,
                "name": name,
                "branch_id": branch.pk,
                "procurement_branch_id": procurement,
                "status": project.status,
            },
        )
        record(
            identity,
            "location.created",
            obj=worksite,
            branch_id=branch.pk,
            data={"kind": worksite.kind, "project_id": project.pk},
        )
        events.publish("project.created", project_id=project.pk, worksite_id=worksite.pk)
        return project

    return execute_once(
        actor_id=principal.user_id,
        key=idempotency_key,
        operation="project.create",
        payload=payload,
        execute=execute,
        replay=lambda _type, pk: Project.objects.get(pk=pk),
    )


@transactional
def update_project(principal: Principal, project_id: UUID, **changes: Any) -> Project:
    """Change a project; ``code`` is fixed. Its worksite follows a change of ``branch``."""
    _project_change(principal)
    project = Project.objects.get(pk=project_id)
    diff = apply_changes(project, changes, PROJECT_FIELDS)
    if not diff:
        return project
    project.full_clean()
    if "branch_id" in diff:
        _active_branch(project.branch_id, "branch")
    if "procurement_branch_id" in diff:
        _active_branch(project.procurement_branch_id, "procurement_branch")
    project.save()
    if "branch_id" in diff:
        Location.objects.filter(project=project).update(branch_id=project.branch_id)
    record(
        principal.audit_identity,
        "project.updated",
        obj=project,
        branch_id=project.branch_id,
        data=diff,
    )
    events.publish("project.updated", project_id=project.pk, fields=sorted(diff))
    return project


# --- assignments --------------------------------------------------------------------


@transactional
def assign_to_project(
    principal: Principal,
    *,
    project_id: UUID,
    user_id: UUID,
    kind: str,
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
    idempotency_key: str | None = None,
) -> ProjectAssignment:
    """Share a project with a site manager or a partner (S4)."""
    lock_for_user_change(principal, user_id, PROJECT_MANAGE)
    payload = {
        "project_id": project_id,
        "user_id": user_id,
        "kind": kind,
        "valid_from": valid_from,
        "valid_until": valid_until,
    }

    def execute() -> ProjectAssignment:
        assignment = ProjectAssignment(
            project=Project.objects.get(pk=project_id),
            user_id=user_id,
            kind=kind,
            valid_from=valid_from or db_now(),
            valid_until=valid_until,
        )
        assignment.full_clean()
        assignment.save()
        record(
            principal.audit_identity,
            "project_assignment.granted",
            obj=assignment,
            branch_id=assignment.project.branch_id,
            data={
                "project_id": project_id,
                "user_id": user_id,
                "kind": kind,
                "valid_from": assignment.valid_from,
                "valid_until": valid_until,
            },
        )
        events.publish("project_assignment.granted", assignment_id=assignment.pk, user_id=user_id)
        return assignment

    return execute_once(
        actor_id=principal.user_id,
        key=idempotency_key,
        operation="project_assignment.grant",
        payload=payload,
        execute=execute,
        replay=lambda _type, pk: ProjectAssignment.objects.get(pk=pk),
    )


def _open_assignment(assignment_id: UUID) -> ProjectAssignment:
    assignment = ProjectAssignment.objects.select_related("project").get(pk=assignment_id)
    if assignment.revoked_at is not None:
        raise ValidationError(_("This assignment is already revoked."), code="revoked")
    return assignment


def _assignment_user(assignment_id: UUID) -> UUID:
    # Discover the user to lock; an assignment's user never changes, so no re-check.
    user_id: UUID = ProjectAssignment.objects.values_list("user_id", flat=True).get(
        pk=assignment_id
    )
    return user_id


@transactional
def revoke_project_assignment(principal: Principal, assignment_id: UUID) -> ProjectAssignment:
    """Revoke an assignment now (database clock); it is never deleted."""
    user_id = _assignment_user(assignment_id)
    actor = lock_for_user_change(principal, user_id, PROJECT_MANAGE)
    assignment = _open_assignment(assignment_id)
    assignment.revoked_at = db_now()
    assignment.revoked_by = actor
    assignment.save(update_fields=["revoked_at", "revoked_by", "updated_at"])
    record(
        principal.audit_identity,
        "project_assignment.revoked",
        obj=assignment,
        branch_id=assignment.project.branch_id,
        data={"project_id": assignment.project_id, "user_id": user_id, "kind": assignment.kind},
    )
    events.publish("project_assignment.revoked", assignment_id=assignment.pk, user_id=user_id)
    return assignment


@transactional
def change_assignment_validity(
    principal: Principal,
    assignment_id: UUID,
    *,
    valid_from: datetime,
    valid_until: datetime | None,
) -> ProjectAssignment:
    user_id = _assignment_user(assignment_id)
    lock_for_user_change(principal, user_id, PROJECT_MANAGE)
    assignment = _open_assignment(assignment_id)
    diff = apply_changes(
        assignment,
        {"valid_from": valid_from, "valid_until": valid_until},
        {"valid_from", "valid_until"},
    )
    if not diff:
        return assignment
    assignment.full_clean()
    assignment.save(update_fields=["valid_from", "valid_until", "updated_at"])
    record(
        principal.audit_identity,
        "project_assignment.validity_changed",
        obj=assignment,
        branch_id=assignment.project.branch_id,
        data=diff,
    )
    events.publish(
        "project_assignment.validity_changed", assignment_id=assignment.pk, user_id=user_id
    )
    return assignment
