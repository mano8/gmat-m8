"""The worker's transactions (plan §5 "Email: outbox and worker", §4.7).

Every function acts for the worker's ``SystemPrincipal`` (``system:worker``) and starts with
``authorize_in_txn``, which admits only ``outbox.send`` and ``housekeeping.<task>``: the
worker holds no business capability. When it checks recipients, it evaluates *their* access
at the database clock (``apps.notifications.eligibility``).

- ``claim_next``: in one short transaction, lock one due ``QUEUED`` message (a dispatch: its
  order ``FOR SHARE`` at L4 first, then the row at L4b; anything else: the oldest row
  ``FOR UPDATE SKIP LOCKED``), re-check expiry and eligibility, insert an ``EmailAttempt``,
  point ``current_attempt`` at it and set ``SENDING``. The commit happens before any network
  I/O; the returned ``Claim`` carries what the worker needs to send.
- ``record_outcome``: in a new transaction (a dispatch: order, then row), conditional on
  ``current_attempt`` still being that attempt; applies the retry policy of the kind.
- A dispatch's attempts are audited by its source (``record_attempt``) when they start and
  when they end, abandoned and late outcomes included (M5.3).
- ``reconcile_abandoned``: a ``SENDING`` message whose attempt started more than the send
  deadline + ``OUTBOX_ABANDON_GRACE`` ago becomes ``AMBIGUOUS`` (conditional on the attempt
  id, so a newer attempt is never touched). A late outcome of that attempt may still turn it
  into ``SENT`` or ``FAILED``.
- ``run_housekeeping``: the tasks of ``apps.notifications.housekeeping``, one transaction
  and one ``housekeeping.<name>`` authorization each.

Sensitive bodies are wiped as soon as the message is sent or can no longer be sent; one
that is still queued at ``expires_at`` is wiped and expired by housekeeping.
"""

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.utils import make_msgid
from types import MappingProxyType
from typing import Any, cast
from uuid import UUID

from cryptography.fernet import InvalidToken
from django.conf import settings
from django.db import DatabaseError, transaction
from django.db.models import Min, QuerySet
from django.db.models.functions import Now

from apps.access.authorization import authorize_in_txn
from apps.access.principals import SessionPrincipal, SystemPrincipal
from apps.access.roles import Capability
from apps.audit.recorder import record
from apps.core.db import db_now
from apps.core.locking import LockLevel, LockOrder, run_in_transaction, transactional
from apps.notifications import eligibility, housekeeping
from apps.notifications.crypto import unseal
from apps.notifications.dispatch import DispatchSource, dispatch_source
from apps.notifications.models import (
    DISPATCH_KINDS,
    EmailAttempt,
    EmailOutbox,
    Kind,
    NotificationPreference,
    Outcome,
    Status,
)
from apps.notifications.outbox import sender_domain

logger = logging.getLogger(__name__)

OUTBOX_SEND = "outbox.send"


@dataclass(frozen=True)
class RetryPolicy:
    """``FAILED`` is retried while fewer than ``max_attempts`` were made; ``AMBIGUOUS`` only
    ``ambiguous_retries`` times (each retry re-checks eligibility at its claim)."""

    max_attempts: int
    ambiguous_retries: int


# Plan §5: notifications retry FAILED up to 5 attempts and AMBIGUOUS once (a duplicate
# notification is acceptable); supplier dispatches are never resent after AMBIGUOUS (M5.3).
# Security emails retry FAILED within their hour and are not resent after AMBIGUOUS.
RETRY_POLICY: Mapping[str, RetryPolicy] = MappingProxyType(
    {
        Kind.NOTIFICATION: RetryPolicy(max_attempts=5, ambiguous_retries=1),
        Kind.SECURITY: RetryPolicy(max_attempts=5, ambiguous_retries=0),
        Kind.PO_ORDER: RetryPolicy(max_attempts=5, ambiguous_retries=0),
        Kind.PO_CANCELLATION: RetryPolicy(max_attempts=5, ambiguous_retries=0),
    }
)
RETRY_BASE_DELAY = 60  # seconds before the 2nd attempt; doubles for each one after
RETRY_MAX_DELAY = 30 * 60

ABANDONED_ERROR = "no outcome within the send deadline"


@dataclass(frozen=True)
class Claim:
    """A message claimed for sending: the attempt to report on and the frozen content."""

    message_id: UUID
    attempt_id: UUID
    attempt_number: int
    kind: str
    from_email: str
    to: tuple[str, ...]
    bcc: tuple[str, ...]
    subject: str
    header_message_id: str
    body: str = field(repr=False)
    html_body: str = field(repr=False)


@dataclass(frozen=True)
class Settled:
    """A message taken off the queue without sending (expired, suppressed, refused...)."""

    message_id: UUID
    status: str


@dataclass(frozen=True)
class Reconciled:
    abandoned: int
    # Seconds until the oldest attempt still SENDING would be abandoned; None if none is.
    next_due_in: float | None


def abandon_after() -> timedelta:
    """The send deadline plus the grace before an attempt counts as abandoned (plan §5)."""
    return timedelta(
        seconds=int(settings.OUTBOX_SEND_DEADLINE) + int(settings.OUTBOX_ABANDON_GRACE)
    )


def retry_delay(attempts: int) -> timedelta:
    return timedelta(seconds=min(RETRY_BASE_DELAY * 2 ** max(attempts - 1, 0), RETRY_MAX_DELAY))


# --- claim ---------------------------------------------------------------------------------


def _due() -> QuerySet[EmailOutbox]:
    return EmailOutbox.objects.filter(status=Status.QUEUED, not_before__lte=Now())


def _wipe(message: EmailOutbox) -> None:
    if message.sensitive and message.wiped_at is None:
        message.body = ""
        message.html_body = ""
        message.wiped_at = Now()


def _settle(message: EmailOutbox, status: Status, error: str = "") -> Settled:
    """End ``message`` without sending it."""
    message.status = status
    message.last_error = error
    _wipe(message)
    message.save(update_fields=["status", "last_error", "body", "html_body", "wiped_at"])
    return Settled(message.pk, status)


@dataclass(frozen=True)
class _Recipients:
    """Who a claimed message goes to: notification recipients by id, and the addresses."""

    users: list[UUID]
    to: tuple[str, ...]
    bcc: tuple[str, ...] = ()


def _recipients(message: EmailOutbox, source: DispatchSource | None) -> _Recipients | Status:
    """The eligibility check of the claim: the recipients, or the status the message gets."""
    if message.kind == Kind.NOTIFICATION:
        recipients = eligibility.eligible_recipients(message)
        if not recipients:
            return Status.SUPPRESSED
        addresses = tuple(user.email for user in recipients)
        to, bcc = (addresses, ()) if len(addresses) == 1 else ((), addresses)
        return _Recipients([user.pk for user in recipients], to, bcc)
    if message.kind == Kind.SECURITY:
        if not eligibility.security_target_ok(message):
            return Status.SUPPRESSED
    else:
        # Dispatches are only claimed while a source is registered (``_claim``).
        refusal = cast(DispatchSource, source).refusal(message)
        if refusal is not None:
            return refusal
    return _Recipients([], (message.recipient_address,))


def _bodies(message: EmailOutbox) -> tuple[str, str]:
    """The text and HTML bodies, unsealed (``InvalidToken`` when no key opens them)."""
    if not message.sensitive:
        return message.body, message.html_body
    return unseal(message.body), unseal(message.html_body)


def _start_attempt(
    principal: SystemPrincipal,
    message: EmailOutbox,
    source: DispatchSource | None,
    users: list[UUID],
    worker: str,
) -> EmailAttempt:
    """The attempt, committed with ``SENDING`` before any I/O; a dispatch's is audited."""
    attempt = EmailAttempt.objects.create(
        message=message, number=message.attempts + 1, recipient_users=users, worker=worker
    )
    message.status = Status.SENDING
    message.current_attempt = attempt
    message.attempts = attempt.number
    if not message.message_id:
        message.message_id = make_msgid(domain=sender_domain(message.from_email))
    message.save(update_fields=["status", "current_attempt", "attempts", "message_id"])
    if message.is_dispatch:
        cast(DispatchSource, source).record_attempt(principal, message, attempt)
    return attempt


def _prepare(
    principal: SystemPrincipal, message: EmailOutbox, source: DispatchSource | None, worker: str
) -> Claim | Settled:
    """Expiry and eligibility under the row lock; then the attempt, before any I/O."""
    if message.expires_at is not None and message.expires_at <= db_now():
        return _settle(message, Status.EXPIRED)
    recipients = _recipients(message, source)
    if not isinstance(recipients, _Recipients):
        return _settle(message, recipients)
    try:
        body, html_body = _bodies(message)
    except InvalidToken:
        return _settle(message, Status.FAILED, "the body cannot be decrypted with any key")
    attempt = _start_attempt(principal, message, source, recipients.users, worker)
    return Claim(
        message_id=message.pk,
        attempt_id=attempt.pk,
        attempt_number=attempt.number,
        kind=message.kind,
        from_email=message.from_email,
        to=recipients.to,
        bcc=recipients.bcc,
        subject=message.subject,
        header_message_id=message.message_id,
        body=body,
        html_body=html_body,
    )


def _claim(
    principal: SystemPrincipal, source: DispatchSource | None, worker: str
) -> Claim | Settled | None:
    locks = LockOrder()
    authorize_in_txn(principal, OUTBOX_SEND, locks=locks)
    candidates = _due().order_by("not_before", "id")
    if source is None:
        candidates = candidates.exclude(kind__in=DISPATCH_KINDS)
    candidate = candidates.values("pk", "kind", "source_po").first()
    if candidate is None:
        return None
    if candidate["kind"] in DISPATCH_KINDS:
        # Discover → lock → verify (plan §4.7): the order at L4, then its row at L4b.
        cast(DispatchSource, source).lock_order(locks, candidate["source_po"])
        locks.enter(LockLevel.PO_OUTBOX)
        message = _due().filter(pk=candidate["pk"]).select_for_update(skip_locked=True).first()
    else:
        message = (
            _due()
            .exclude(kind__in=DISPATCH_KINDS)
            .order_by("not_before", "id")
            .select_for_update(skip_locked=True)
            .first()
        )
    if message is None:
        return None  # taken by another worker in the meantime
    try:
        with transaction.atomic():
            return _prepare(principal, message, source, worker)
    except DatabaseError:
        raise
    except Exception:
        # A message whose checks fail must not block the queue behind it forever.
        logger.exception("Claiming outbox message %s failed; it is set FAILED", message.pk)
        return _settle(message, Status.FAILED, "internal error while claiming")


def claim_next(principal: SystemPrincipal, *, worker: str = "") -> Claim | Settled | None:
    """Claim the next due message; ``None`` when there is nothing to claim right now."""
    source = dispatch_source()
    return run_in_transaction(lambda: _claim(principal, source, worker))


# --- outcomes ------------------------------------------------------------------------------


def _record_attempt(
    principal: SystemPrincipal, message: EmailOutbox | None, attempt: EmailAttempt
) -> None:
    """Audit a dispatch attempt's outcome (``_lock_message`` refused one without a source)."""
    if message is not None and message.is_dispatch:
        cast(DispatchSource, dispatch_source()).record_attempt(principal, message, attempt)


def _lock_message(locks: LockOrder, message_id: UUID) -> EmailOutbox | None:
    """The message row ``FOR UPDATE``; a dispatch's order first (L4 → L4b)."""
    found = EmailOutbox.objects.filter(pk=message_id).values("kind", "source_po").first()
    if found is None:
        return None
    if found["kind"] in DISPATCH_KINDS:
        source = dispatch_source()
        if source is None:
            raise RuntimeError("A dispatch outcome needs the purchasing app's dispatch source")
        source.lock_order(locks, found["source_po"])
        locks.enter(LockLevel.PO_OUTBOX)
    return EmailOutbox.objects.select_for_update().filter(pk=message_id).first()


def _expired(message: EmailOutbox) -> bool:
    return message.expires_at is not None and message.expires_at <= db_now()


def _apply(message: EmailOutbox, attempt: EmailAttempt, outcome: Outcome, locks: LockOrder) -> None:
    """Set the message's status from its current attempt's outcome and the retry policy."""
    policy = RETRY_POLICY[message.kind]
    message.last_error = attempt.error
    if outcome == Outcome.SENT:
        message.status = Status.SENT
        message.sent_at = Now()
        message.last_error = ""
    else:
        retry = message.attempts < policy.max_attempts and not _expired(message)
        if outcome == Outcome.AMBIGUOUS:
            ambiguous = message.attempt_set.filter(outcome=Outcome.AMBIGUOUS).count()
            retry = retry and ambiguous <= policy.ambiguous_retries
        if retry:
            message.status = Status.QUEUED
            message.not_before = Now() + retry_delay(message.attempts)
        else:
            message.status = Status.FAILED if outcome == Outcome.FAILED else Status.AMBIGUOUS
    if message.status != Status.QUEUED:
        _wipe(message)
    message.save(
        update_fields=[
            "status",
            "last_error",
            "sent_at",
            "not_before",
            "body",
            "html_body",
            "wiped_at",
        ]
    )
    if message.is_dispatch and message.status in {Status.SENT, Status.AMBIGUOUS}:
        # ``_lock_message`` refused a dispatch without a source.
        cast(DispatchSource, dispatch_source()).after_outcome(message, locks)


def _record(principal: SystemPrincipal, attempt_id: UUID, outcome: Outcome, error: str) -> bool:
    locks = LockOrder()
    authorize_in_txn(principal, OUTBOX_SEND, locks=locks)
    message_id = (
        EmailAttempt.objects.filter(pk=attempt_id).values_list("message_id", flat=True).first()
    )
    if message_id is None:
        return False
    message = _lock_message(locks, message_id)
    attempt = EmailAttempt.objects.select_for_update().get(pk=attempt_id)
    if attempt.outcome and not attempt.abandoned:
        return False  # this attempt already reported
    late = attempt.abandoned
    attempt.outcome = outcome
    attempt.error = error[:255]
    attempt.abandoned = False
    attempt.finished_at = Now()
    attempt.save(update_fields=["outcome", "error", "abandoned", "finished_at"])
    _record_attempt(principal, message, attempt)
    current = message is not None and message.current_attempt_id == attempt.pk
    allowed = {Status.SENDING} | ({Status.AMBIGUOUS, Status.QUEUED} if late else set())
    if not current or message is None or message.status not in allowed:
        return False  # a newer attempt, or a status set since: never overwritten
    _apply(message, attempt, outcome, locks)
    return True


def record_outcome(
    principal: SystemPrincipal, attempt_id: UUID, outcome: Outcome, *, error: str = ""
) -> bool:
    """Record how ``attempt_id`` ended; return whether the message took the outcome.

    The attempt row always keeps its own outcome. The message changes only while
    ``current_attempt`` is still this attempt (and, for a late outcome after the
    abandoned-attempt reconciliation, while it is still ``AMBIGUOUS`` or queued for retry).
    """
    return run_in_transaction(lambda: _record(principal, attempt_id, outcome, error))


# --- abandoned attempts --------------------------------------------------------------------


def _abandon(principal: SystemPrincipal, message_id: UUID, attempt_id: UUID) -> bool:
    locks = LockOrder()
    authorize_in_txn(principal, OUTBOX_SEND, locks=locks)
    message = _lock_message(locks, message_id)
    if (
        message is None
        or message.status != Status.SENDING
        or message.current_attempt_id != attempt_id
    ):
        return False
    attempt = (
        EmailAttempt.objects.select_for_update()
        .filter(pk=attempt_id, outcome="", started_at__lt=Now() - abandon_after())
        .first()
    )
    if attempt is None:
        return False
    attempt.outcome = Outcome.AMBIGUOUS
    attempt.abandoned = True
    attempt.error = ABANDONED_ERROR
    attempt.finished_at = Now()
    attempt.save(update_fields=["outcome", "abandoned", "error", "finished_at"])
    _record_attempt(principal, message, attempt)
    _apply(message, attempt, Outcome.AMBIGUOUS, locks)
    return True


def _abandon_in_txn(principal: SystemPrincipal, message_id: UUID, attempt_id: UUID) -> bool:
    return run_in_transaction(lambda: _abandon(principal, message_id, attempt_id))


def _next_due_in() -> float | None:
    oldest: datetime | None = EmailOutbox.objects.filter(status=Status.SENDING).aggregate(
        started=Min("current_attempt__started_at")
    )["started"]
    if oldest is None:
        return None
    return max((oldest + abandon_after() - db_now()).total_seconds(), 0.0)


def reconcile_abandoned(principal: SystemPrincipal) -> Reconciled:
    """Turn abandoned ``SENDING`` messages ``AMBIGUOUS`` (at start-up and every minute)."""
    stale = (
        EmailOutbox.objects.filter(
            status=Status.SENDING,
            current_attempt__outcome="",
            current_attempt__started_at__lt=Now() - abandon_after(),
        )
        .order_by("pk")
        .values_list("pk", "current_attempt_id")
    )
    abandoned = 0
    for message_id, attempt_id in list(stale):
        if _abandon_in_txn(principal, message_id, attempt_id):
            abandoned += 1
    return Reconciled(abandoned=abandoned, next_due_in=_next_due_in())


# --- housekeeping --------------------------------------------------------------------------


def _housekeep(principal: SystemPrincipal, name: str, task: Callable[[], Any]) -> int:
    authorize_in_txn(principal, f"housekeeping.{name}", locks=LockOrder())
    return int(task())


def _housekeep_in_txn(principal: SystemPrincipal, name: str, task: Callable[[], Any]) -> int:
    return run_in_transaction(lambda: _housekeep(principal, name, task))


def run_housekeeping(principal: SystemPrincipal) -> dict[str, int]:
    """Run every registered housekeeping task; return the rows each one touched."""
    return {
        name: _housekeep_in_txn(principal, name, task)
        for name, task in housekeeping.tasks().items()
    }


# --- the user's own choice about business email (M7.1) -------------------------------------


@transactional
def set_email_opt_out(principal: SessionPrincipal, opt_out: bool) -> NotificationPreference:
    """Turn the signed-in user's business email off (``True``) or on (``False``).

    Any current member may do this for themselves: ``catalog.view`` is the capability every
    role holds, and the row is always the principal's own. Security email and supplier
    dispatches are not affected. A change that alters nothing writes nothing.
    """
    user = authorize_in_txn(principal, Capability.CATALOG_VIEW, locks=LockOrder())
    preference, created = NotificationPreference.objects.select_for_update().get_or_create(
        user=user, defaults={"email_opt_out": opt_out}
    )
    changed = preference.email_opt_out != opt_out if not created else opt_out
    if not changed:
        return preference
    if not created:
        preference.email_opt_out = opt_out
        preference.save(update_fields=["email_opt_out", "updated_at"])
    record(
        principal.audit_identity,
        "notification_preference.changed",
        obj=preference,
        data={"email_opt_out": opt_out},
    )
    return preference
