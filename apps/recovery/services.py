"""After a restore, nothing restored may resurrect access or re-send mail (plan §9).

A restore brings back old sessions, epochs, passwords, memberships, invitations, MFA state
and outbox rows. ``post_restore_reconcile`` runs once, before either gate opens, in one
transaction, on the authority of the operator doing the recovery. The host part of step 1
(a new ``SECRET_KEY`` with no fallback) is ``recover.sh``'s; with the writers stopped there
is nothing to race, but the locks are the usual ones.

1. Every session row goes and every ``session_epoch`` moves on.
2. Every pending invitation and email change is revoked; every unsent security email is
   wiped and expired.
3. Every restored set of recovery codes is deleted (allauth derives them from a stored seed
   and tracks used ones in a mask, so a restore revives consumed or replaced codes, F52). The
   owners generate a new set: privileged accounts must (``apps.accounts.mfa``).
4. The outbox is quarantined (F44): anything unsettled, unless the failed database recorded a
   terminal outcome for the same row, which it then takes. A human releases what is left
   (``manage outbox_quarantine``); nothing is sent automatically.
5. With a failed state: its later access and credential events are replayed
   (``apps.recovery.replay``).
6. Without one (the VPS is gone), the accounts in the gap cannot be known (Q23): every
   privileged account but the operator's is suspended until the maintainer confirms it
   (``confirm_suspended_account``), and the ones the maintainer knows about are offboarded or
   lose their credentials (``Gap``).
7. The returned report says what happened; the audit trail keeps its counts.
"""

from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from allauth.mfa.models import Authenticator
from allauth.usersessions.models import UserSession
from django.contrib.sessions.models import Session
from django.core.exceptions import ValidationError
from django.db.models import F, Q
from django.utils.translation import gettext_lazy as _

from apps.access.authorization import authorize_in_txn
from apps.access.exceptions import AccessDenied
from apps.access.generations import lock_access_state, lock_users
from apps.access.policy import (
    active_platform_operator_exists,
    is_platform_operator,
    privileged_accounts,
)
from apps.access.principals import Principal
from apps.access.roles import Capability
from apps.accounts.models import EmailChangeRequest, Invitation, User
from apps.audit.models import AuditEvent
from apps.audit.recorder import record
from apps.core.db import db_now
from apps.core.locking import LockLevel, LockMode, LockOrder, transactional
from apps.notifications.models import DISPATCH_KINDS, EmailAttempt, EmailOutbox, Outcome, Status
from apps.organizations.services import lock_for_user_change
from apps.purchasing.models import PurchaseOrder
from apps.recovery.failed_state import FailedState, OutboxOutcome
from apps.recovery.replay import SUSPENDED_ACTION, Replay, offboard, replay_events, suspend

REASON_RESTORE = "database restored"
UNSETTLED = (Status.QUEUED, Status.SENDING, Status.FAILED, Status.AMBIGUOUS)
# The row reached one of these in the failed database: no human needs to decide on it. Plan §9
# names the first four; an expired message can no longer be sent either.
TERMINAL = frozenset(
    {Status.SENT, Status.CANCELLED, Status.SUPERSEDED, Status.SUPPRESSED, Status.EXPIRED}
)
ATTEMPT_ERROR = "restored from a backup"
NO_STATE_REASON = "restored without a preserved database (Q23)"


@dataclass(frozen=True)
class Gap:
    """What the maintainer knows about the gap when the failed database is lost (Q23)."""

    offboarded: frozenset[str] = frozenset()
    credentials_changed: frozenset[str] = frozenset()


@dataclass
class Report:
    failed_state_used: bool
    sessions_deleted: int = 0
    session_epochs_bumped: int = 0
    invitations_revoked: int = 0
    email_changes_revoked: int = 0
    security_emails_expired: int = 0
    recovery_code_sets_invalidated: int = 0
    open_attempts_closed: int = 0
    outbox_quarantined: dict[str, int] = field(default_factory=dict)
    outbox_outcome_taken: dict[str, int] = field(default_factory=dict)
    replayed: dict[str, int] = field(default_factory=dict)
    already_so: dict[str, int] = field(default_factory=dict)
    not_replayed: dict[str, int] = field(default_factory=dict)
    gap_offboarded: int = 0
    gap_credentials_reset: int = 0
    suspended: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _require_operator(actor: User) -> None:
    """A platform operator does this. Only an installation without one (its first administrator
    came without ``--superuser``) lets a group administrator: a disaster needs a way out."""
    if not is_platform_operator(actor) and active_platform_operator_exists():
        raise AccessDenied("A restore is reconciled by a platform operator")


def _end_sessions(report: Report) -> None:
    report.sessions_deleted, _details = Session.objects.all().delete()
    UserSession.objects.all().delete()
    report.session_epochs_bumped = User.objects.update(session_epoch=F("session_epoch") + 1)


def _revoke_tokens(report: Report, now: datetime) -> None:
    report.invitations_revoked = Invitation.objects.filter(
        accepted_at__isnull=True, revoked_at__isnull=True
    ).update(revoked_at=now, revoke_reason=REASON_RESTORE, updated_at=now)
    report.email_changes_revoked = EmailChangeRequest.objects.filter(
        applied_at__isnull=True, cancelled_at__isnull=True, revoked_at__isnull=True
    ).update(revoked_at=now, revoke_reason=REASON_RESTORE, updated_at=now)


def _expire_security_mail(report: Report, now: datetime) -> None:
    unsent = EmailOutbox.objects.filter(sensitive=True, status__in=(*UNSETTLED, Status.QUARANTINED))
    report.security_emails_expired = unsent.count()
    unsent.filter(wiped_at__isnull=True).update(
        status=Status.EXPIRED, body="", html_body="", wiped_at=now, updated_at=now
    )
    unsent.update(status=Status.EXPIRED, updated_at=now)


def _close_open_attempts(report: Report, now: datetime) -> None:
    """A restored attempt in flight has no worker left to report it; it may have been sent."""
    report.open_attempts_closed = EmailAttempt.objects.filter(outcome="").update(
        outcome=Outcome.AMBIGUOUS,
        finished_at=now,
        abandoned=True,
        error=ATTEMPT_ERROR,
        updated_at=now,
    )


def _lock_dispatches(locks: LockOrder) -> None:
    """Dispatches lock their order first (L4 → L4b), as cancel and amend do."""
    dispatches = EmailOutbox.objects.filter(kind__in=DISPATCH_KINDS, status__in=UNSETTLED)
    orders = dispatches.order_by().values_list("source_po_id", flat=True).distinct()
    locks.lock(LockLevel.PO_HEADERS, PurchaseOrder, set(orders))
    locks.lock(LockLevel.PO_OUTBOX, EmailOutbox, set(dispatches.values_list("pk", flat=True)))


def _take_outcomes(report: Report, outcomes: tuple[OutboxOutcome, ...], now: datetime) -> None:
    by_id = {item.id: item for item in outcomes if item.status in TERMINAL}
    taken: Counter[str] = Counter()
    for message in EmailOutbox.objects.filter(status__in=UNSETTLED, pk__in=by_id):
        outcome = by_id[message.pk]
        EmailOutbox.objects.filter(pk=message.pk).update(
            status=outcome.status, sent_at=outcome.sent_at, updated_at=now
        )
        taken[outcome.status] += 1
    report.outbox_outcome_taken = dict(taken)


def _quarantine(report: Report, now: datetime) -> None:
    unsettled = EmailOutbox.objects.filter(status__in=UNSETTLED)
    report.outbox_quarantined = dict(Counter(unsettled.order_by().values_list("kind", flat=True)))
    unsettled.update(status=Status.QUARANTINED, updated_at=now)


def _replay(report: Report, state: FailedState, replay: Replay) -> None:
    ids = [row.id for row in state.audit]
    restored = set(AuditEvent.objects.filter(pk__in=ids).values_list("pk", flat=True))
    replay_events(state.audit, replay, restored=restored)
    report.replayed = dict(replay.applied)
    report.already_so = dict(replay.already)
    report.not_replayed = dict(replay.not_replayed)


def _accounts(addresses: frozenset[str]) -> list[User]:
    """The named accounts; a typo must not leave someone active, so an unknown one is refused."""
    wanted = {address.strip().lower() for address in addresses}
    found = list(User.objects.filter(email__in=wanted).order_by("pk"))
    unknown = wanted - {user.email for user in found}
    if unknown:
        raise ValidationError(
            _("These addresses are not accounts of the restored database: %(addresses)s"),
            code="unknown_address",
            params={"addresses": ", ".join(sorted(unknown))},
        )
    return found


def _remove_credentials(user: User) -> None:
    user.set_unusable_password()
    user.save(update_fields=["password"])
    Authenticator.objects.filter(user=user).delete()


def _apply_gap(report: Report, gap: Gap, replay: Replay) -> None:
    gone = _accounts(gap.offboarded)
    changed = _accounts(gap.credentials_changed)
    for user in gone:
        offboard(user, when=replay.now, actor=replay.actor, now=replay.now)
    for user in changed:
        _remove_credentials(user)
    report.gap_offboarded, report.gap_credentials_reset = len(gone), len(changed)
    for user in privileged_accounts().exclude(pk=replay.actor.pk).order_by("email"):
        suspend(replay, user, NO_STATE_REASON)


def _invalidate_recovery_codes(report: Report) -> None:
    report.recovery_code_sets_invalidated, _details = Authenticator.objects.filter(
        type=Authenticator.Type.RECOVERY_CODES
    ).delete()
    # After the replay and the gap: an account that lost its factors enrolls again, and
    # enrolling gives it codes.
    User.objects.filter(pk__in=Authenticator.objects.values("user_id")).update(
        recovery_codes_owed=True
    )


@transactional
def post_restore_reconcile(
    principal: Principal, *, failed_state: FailedState | None, gap: Gap | None = None
) -> Report:
    """Steps 1-7 of the module note, on a database that was just restored."""
    if failed_state is not None and gap is not None:
        raise ValidationError(
            _("A preserved database says what happened: there is no gap to list."),
            code="gap_with_state",
        )
    locks = LockOrder()
    lock_access_state(locks, mode=LockMode.UPDATE)
    lock_users(locks, User.objects.values_list("pk", flat=True), mode=LockMode.NO_KEY_UPDATE)
    actor = authorize_in_txn(principal, Capability.ORG_MANAGE, locks=locks)
    _require_operator(actor)
    now = db_now()
    report = Report(failed_state_used=failed_state is not None)
    _end_sessions(report)
    _revoke_tokens(report, now)
    _expire_security_mail(report, now)
    _lock_dispatches(locks)
    _close_open_attempts(report, now)
    if failed_state is not None:
        _take_outcomes(report, failed_state.outbox, now)
    _quarantine(report, now)
    replay = Replay(identity=principal.audit_identity, actor=actor, now=now)
    if failed_state is not None:
        _replay(report, failed_state, replay)
    else:
        _apply_gap(report, gap or Gap(), replay)
    report.suspended = replay.suspended
    _invalidate_recovery_codes(report)
    record(principal.audit_identity, "recovery.reconciled", data=report.as_dict())
    return report


def _suspended(user_id: UUID) -> User:
    user = User.objects.get(pk=user_id)
    was_suspended = AuditEvent.objects.filter(
        action=SUSPENDED_ACTION, object_type=user._meta.label_lower, object_id=user.pk
    ).exists()
    if user.is_active or user.offboarded_at is not None or not was_suspended:
        raise ValidationError(_("This account is not suspended."), code="not_suspended")
    return user


@transactional
def confirm_suspended_account(
    principal: Principal, user_id: UUID, *, credentials_changed: bool = False
) -> User:
    """The maintainer vouches for an account a restore suspended (Q23): it signs in again.

    ``credentials_changed``: its password or factors changed in the gap, so what was restored
    is not trusted; it resets the password and enrolls again.
    """
    lock_for_user_change(principal, user_id, Capability.ORG_MANAGE)
    user = _suspended(user_id)
    if credentials_changed:
        _remove_credentials(user)
    User.objects.filter(pk=user.pk).update(is_active=True)
    record(
        principal.audit_identity,
        "recovery.account_confirmed",
        obj=user,
        data={"credentials_changed": credentials_changed},
    )
    return User.objects.get(pk=user_id)


def suspended_accounts() -> list[User]:
    """The accounts still waiting for ``confirm_suspended_account``."""
    ids = AuditEvent.objects.filter(action=SUSPENDED_ACTION).values_list("object_id", flat=True)
    return list(
        User.objects.filter(Q(pk__in=ids), is_active=False, offboarded_at__isnull=True).order_by(
            "email"
        )
    )
