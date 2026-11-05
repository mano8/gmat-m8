"""Replay of what the restore undid: access-reducing and credential events (plan §9, step 5).

A restore brings back the rows as they were at the snapshot. The failed database's audit log
says what happened after it; an event whose id the restored audit table lacks is newer than
the snapshot (a transaction still open at the snapshot included, which a timestamp would
miss). In log order (UUIDv7), each one that took access or a credential away is applied again:

- memberships and project assignments revoked, or their validity narrowed;
- accounts offboarded, companies and branches deactivated, projects put on hold or closed;
- email changes applied;
- second factors reset or removed;
- passwords changed: the old hash may be a stolen one, so the account gets an unusable
  password and resets it after reopening.

Everything else, the grants above all, is reported as not replayed and entered again by a
human: a restore never gives anyone more access than the snapshot had. Direct writes under
the caller's authorization and locks: the services' preconditions held when these happened.
"""

from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from allauth.mfa.models import Authenticator
from django.db.models import Model
from django.utils.dateparse import parse_datetime

from apps.accounts.models import User
from apps.audit.recorder import AuditIdentity, record
from apps.organizations.models import Branch, Company, Membership
from apps.projects.models import Project, ProjectAssignment
from apps.recovery.failed_state import AuditRow

SUSPENDED_ACTION = "recovery.account_suspended"
EVENT_REPLAYED = "recovery.event_replayed"


@dataclass
class Replay:
    """The state of one replay, and what the report says about it."""

    identity: AuditIdentity
    actor: User
    now: datetime
    applied: Counter[str] = field(default_factory=Counter)
    already: Counter[str] = field(default_factory=Counter)
    not_replayed: Counter[str] = field(default_factory=Counter)
    suspended: list[str] = field(default_factory=list)


class _NotRelevant(Exception):
    """This event of a replayed action reduced nothing (a renamed company)."""


def revoke_grants(user_id: UUID, *, when: datetime, actor: User, now: datetime) -> None:
    """Revoke every open membership and project assignment of an account."""
    for model in (Membership, ProjectAssignment):
        model.objects.filter(user_id=user_id, revoked_at__isnull=True).update(
            revoked_at=when, revoked_by=actor, updated_at=now
        )


def offboard(user: User, *, when: datetime, actor: User, now: datetime) -> None:
    """Deactivate an account for good and revoke its grants; its tokens are already revoked."""
    User.objects.filter(pk=user.pk).update(is_active=False, offboarded_at=when)
    revoke_grants(user.pk, when=when, actor=actor, now=now)


def suspend(replay: Replay, user: User, reason: str) -> None:
    """Inactive, not offboarded, until the maintainer confirms the account (Q23)."""
    User.objects.filter(pk=user.pk).update(is_active=False)
    record(replay.identity, SUSPENDED_ACTION, obj=user, data={"reason": reason})
    replay.suspended.append(user.email)


def _revoke(model: type[Model], row: AuditRow, replay: Replay) -> bool:
    return bool(
        model._default_manager.filter(pk=row.object_id, revoked_at__isnull=True).update(
            revoked_at=row.occurred_at, revoked_by=replay.actor, updated_at=replay.now
        )
    )


def _revoke_membership(row: AuditRow, replay: Replay) -> bool:
    return _revoke(Membership, row, replay)


def _revoke_assignment(row: AuditRow, replay: Replay) -> bool:
    return _revoke(ProjectAssignment, row, replay)


def _changed_to(row: AuditRow, name: str) -> datetime | None:
    value = row.data.get(name, {}).get("to")
    return parse_datetime(value) if value else None


def _narrow(model: type[Model], row: AuditRow, replay: Replay) -> bool:
    """Apply a validity change where it shortens the window; a longer one is not replayed."""
    grant: Any = model._default_manager.filter(pk=row.object_id, revoked_at__isnull=True).first()
    if grant is None:
        return False
    until = _changed_to(row, "valid_until")
    start = _changed_to(row, "valid_from")
    new_until = min(filter(None, (grant.valid_until, until)), default=None)
    new_from = max(filter(None, (grant.valid_from, start)))
    if (new_until, new_from) == (grant.valid_until, grant.valid_from):
        return False
    if new_until is not None and new_until <= new_from:
        return _revoke(model, row, replay)
    return bool(
        model._default_manager.filter(pk=grant.pk).update(
            valid_until=new_until, valid_from=new_from, updated_at=replay.now
        )
    )


def _narrow_membership(row: AuditRow, replay: Replay) -> bool:
    return _narrow(Membership, row, replay)


def _narrow_assignment(row: AuditRow, replay: Replay) -> bool:
    return _narrow(ProjectAssignment, row, replay)


def _account(user_id: Any) -> User | None:
    return User.objects.filter(pk=user_id).first() if user_id else None


def _offboard(row: AuditRow, replay: Replay) -> bool:
    user = _account(row.object_id)
    if user is None or user.offboarded_at is not None:
        return False
    offboard(user, when=row.occurred_at, actor=replay.actor, now=replay.now)
    return True


def _reset_factors(row: AuditRow, replay: Replay) -> bool:
    removed, _ = Authenticator.objects.filter(user_id=row.object_id).delete()
    return removed > 0


def _remove_factor(row: AuditRow, replay: Replay) -> bool:
    removed, _ = Authenticator.objects.filter(
        user_id=row.object_id, type=row.data.get("type")
    ).delete()
    return removed > 0


def _distrust_password(row: AuditRow, replay: Replay) -> bool:
    user = _account(row.object_id)
    if user is None:
        return False
    user.set_unusable_password()
    user.save(update_fields=["password"])
    return True


def _apply_email_change(row: AuditRow, replay: Replay) -> bool:
    """The old address must lose the account. If another restored account holds the new one
    the change cannot be applied, so the account waits for the maintainer."""
    user = _account(row.data.get("user_id"))
    target = str(row.data.get("to", ""))
    if user is None or user.email == target:
        return False
    if User.objects.exclude(pk=user.pk).filter(email=target).exists():
        suspend(replay, user, "email change after the snapshot could not be applied")
        return True
    user.email = target
    user.save(update_fields=["email"])
    user.emailaddress_set.all().delete()
    user.emailaddress_set.create(email=target, primary=True, verified=True)
    return True


def _deactivate(model: type[Model], row: AuditRow, replay: Replay) -> bool:
    change = row.data.get("is_active")
    if not change or change.get("to") is not False:
        raise _NotRelevant
    return bool(
        model._default_manager.filter(pk=row.object_id, is_active=True).update(
            is_active=False, updated_at=replay.now
        )
    )


def _deactivate_company(row: AuditRow, replay: Replay) -> bool:
    return _deactivate(Company, row, replay)


def _deactivate_branch(row: AuditRow, replay: Replay) -> bool:
    return _deactivate(Branch, row, replay)


def _stop_project(row: AuditRow, replay: Replay) -> bool:
    change = row.data.get("status")
    if not change or change.get("to") == Project.Status.ACTIVE:
        raise _NotRelevant
    matching = Project.objects.filter(pk__in=[row.object_id]).exclude(status=change["to"])
    return bool(matching.update(status=change["to"], updated_at=replay.now))


HANDLERS: dict[str, Callable[[AuditRow, Replay], bool]] = {
    "membership.revoked": _revoke_membership,
    "membership.validity_changed": _narrow_membership,
    "project_assignment.revoked": _revoke_assignment,
    "project_assignment.validity_changed": _narrow_assignment,
    "user.offboarded": _offboard,
    "user.mfa_reset": _reset_factors,
    "user.authenticator_removed": _remove_factor,
    "user.password_changed": _distrust_password,
    "email_change.applied": _apply_email_change,
    "company.updated": _deactivate_company,
    "branch.updated": _deactivate_branch,
    "project.updated": _stop_project,
}


def _apply(row: AuditRow, replay: Replay) -> None:
    handler = HANDLERS.get(row.action)
    if handler is None:
        replay.not_replayed[row.action] += 1
        return
    try:
        changed = handler(row, replay)
    except _NotRelevant:
        replay.not_replayed[row.action] += 1
        return
    if not changed:
        replay.already[row.action] += 1
        return
    replay.applied[row.action] += 1
    record(
        replay.identity,
        EVENT_REPLAYED,
        data={
            "source_event": row.id,
            "source_action": row.action,
            "source_actor": row.actor_label,
            "object_type": row.object_type,
            "object_id": row.object_id,
            "occurred_at": row.occurred_at,
        },
    )


def replay_events(rows: Iterable[AuditRow], replay: Replay, *, restored: set[UUID]) -> None:
    """Apply, in order, the events the restored audit table does not have (``restored``)."""
    for row in rows:
        if row.id not in restored:
            _apply(row, replay)
