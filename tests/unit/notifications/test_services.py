"""The worker's transactions: claim, outcome, reconciliation, housekeeping (plan §5)."""

import logging
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from django.core.cache import cache
from django.db import DatabaseError, transaction
from django.db.models.functions import Now
from django.test import override_settings
from django.utils import timezone

from apps.access.exceptions import AccessDenied
from apps.access.principals import SystemPrincipal
from apps.accounts.models import User
from apps.core.locking import LockOrder
from apps.core.models import IdempotencyRecord
from apps.notifications import housekeeping, services
from apps.notifications.dispatch import dispatch_source, register_dispatch_source
from apps.notifications.models import EmailAttempt, EmailOutbox, Outcome, Status
from apps.notifications.services import (
    Claim,
    Settled,
    claim_next,
    reconcile_abandoned,
    record_outcome,
    run_housekeeping,
)
from apps.organizations.models import Membership
from tests.outbox import SECRET_BODY, FakeOrders
from tests.support import principal_for

pytestmark = pytest.mark.django_db


def claim(system: SystemPrincipal) -> Claim:
    claimed = claim_next(system, worker="test")
    assert isinstance(claimed, Claim), claimed
    return claimed


def make_due(message: EmailOutbox) -> None:
    EmailOutbox.objects.filter(pk=message.pk).update(not_before=Now())


def age(attempt_id: uuid.UUID, seconds: float) -> None:
    EmailAttempt.objects.filter(pk=attempt_id).update(started_at=Now() - timedelta(seconds=seconds))


def reload(message: EmailOutbox) -> EmailOutbox:
    return EmailOutbox.objects.get(pk=message.pk)


class TestPolicy:
    def test_backoff_doubles_and_is_capped(self) -> None:
        assert services.retry_delay(0) == services.retry_delay(1) == timedelta(minutes=1)
        assert services.retry_delay(3) == timedelta(minutes=4)
        assert services.retry_delay(20) == timedelta(minutes=30)

    def test_abandoned_after_the_deadline_and_grace(self) -> None:
        assert services.abandon_after() == timedelta(seconds=90)
        # A worker restarted right after a claim reconciles within two minutes (plan §5).
        assert services.abandon_after().total_seconds() + 0.5 + 2 < 120

    def test_message_id_domain(self) -> None:
        assert services._domain("gmat-m8 <no-reply@app.example.org>") == "app.example.org"
        assert services._domain("no-address") is None


class TestAuthorization:
    def test_only_the_worker_may_claim(
        self, security_mail: Callable[..., EmailOutbox], make_user: Callable[..., User]
    ) -> None:
        security_mail()
        with pytest.raises(AccessDenied):
            claim_next(principal_for(make_user("someone@example.org")))  # type: ignore[arg-type]
        assert EmailOutbox.objects.get().status == Status.QUEUED


class TestClaim:
    def test_nothing_due(self, system: SystemPrincipal) -> None:
        assert claim_next(system) is None

    def test_security_email_attempt_is_committed_with_the_claim(
        self, system: SystemPrincipal, security_mail: Callable[..., EmailOutbox], account: User
    ) -> None:
        message = security_mail()
        claimed = claim(system)
        assert claimed.to == (account.email,)
        assert claimed.bcc == ()
        assert claimed.body == SECRET_BODY
        assert "SECRETTOKEN" not in repr(claimed)
        assert claimed.header_message_id.endswith("@localhost>")
        message = reload(message)
        assert message.status == Status.SENDING
        assert message.attempts == 1
        attempt = message.current_attempt
        assert attempt is not None
        assert (attempt.number, attempt.worker, attempt.outcome) == (1, "test", "")
        assert claim_next(system) is None  # nothing else is due

    def test_oldest_first(
        self, system: SystemPrincipal, security_mail: Callable[..., EmailOutbox]
    ) -> None:
        first, second = security_mail(), security_mail()
        assert claim(system).message_id == first.pk
        assert claim(system).message_id == second.pk

    def test_expired_security_email_is_wiped_not_sent(
        self, system: SystemPrincipal, security_mail: Callable[..., EmailOutbox]
    ) -> None:
        with override_settings(OUTBOX_SENSITIVE_TTL=0):
            message = security_mail()
        assert claim_next(system) == Settled(message.pk, Status.EXPIRED)
        message = reload(message)
        assert (message.status, message.body, message.attempts) == (Status.EXPIRED, "", 0)
        assert message.wiped_at is not None

    def test_security_email_to_a_lost_account_is_suppressed_and_wiped(
        self, system: SystemPrincipal, security_mail: Callable[..., EmailOutbox], account: User
    ) -> None:
        message = security_mail()
        User.objects.filter(pk=account.pk).update(is_active=False)
        assert claim_next(system) == Settled(message.pk, Status.SUPPRESSED)
        assert reload(message).body == ""

    def test_notification_goes_to_eligible_recipients_only(
        self,
        system: SystemPrincipal,
        notification: Callable[..., EmailOutbox],
        manager: User,
        outsider: User,
    ) -> None:
        message = notification(manager, outsider)
        claimed = claim(system)
        assert claimed.to == (manager.email,)
        assert reload(message).current_attempt.recipient_users == [manager.pk]  # type: ignore[union-attr]

    def test_several_recipients_are_blind_copies(
        self,
        system: SystemPrincipal,
        notification: Callable[..., EmailOutbox],
        manager: User,
        org: Any,
        member: Callable[..., User],
    ) -> None:
        from apps.access.roles import Role

        other = member(Role.MATERIAL_MANAGER, email="mm2@example.org", branch=org.a_mad)
        notification(manager, other)
        claimed = claim(system)
        assert claimed.to == ()
        assert sorted(claimed.bcc) == sorted([manager.email, other.email])

    def test_a_recipient_who_lost_access_before_the_claim_gets_nothing(
        self,
        system: SystemPrincipal,
        notification: Callable[..., EmailOutbox],
        manager: User,
    ) -> None:
        message = notification(manager)
        Membership.objects.filter(user=manager).update(revoked_at=timezone.now())
        assert claim_next(system) == Settled(message.pk, Status.SUPPRESSED)
        assert reload(message).attempts == 0

    def test_an_undecryptable_body_fails_without_blocking_the_queue(
        self, system: SystemPrincipal, security_mail: Callable[..., EmailOutbox]
    ) -> None:
        from cryptography.fernet import Fernet

        with override_settings(OUTBOX_ENCRYPTION_KEYS=[Fernet.generate_key().decode()]):
            lost = security_mail()
        readable = security_mail()
        assert claim_next(system) == Settled(lost.pk, Status.FAILED)
        assert reload(lost).body == ""
        assert claim(system).message_id == readable.pk

    def test_a_check_that_crashes_fails_that_message_only(
        self,
        system: SystemPrincipal,
        notification: Callable[..., EmailOutbox],
        manager: User,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        message = notification(manager)

        def crash(message: EmailOutbox) -> list[User]:
            raise KeyError("bug")

        monkeypatch.setattr("apps.notifications.eligibility.eligible_recipients", crash)
        with caplog.at_level(logging.ERROR, logger="apps.notifications.services"):
            assert claim_next(system) == Settled(message.pk, Status.FAILED)
        assert reload(message).last_error == "internal error while claiming"
        assert str(message.pk) in caplog.text

    def test_a_database_error_leaves_the_message_queued(
        self,
        system: SystemPrincipal,
        notification: Callable[..., EmailOutbox],
        manager: User,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        message = notification(manager)

        def broken(message: EmailOutbox) -> list[User]:
            raise DatabaseError("connection lost")

        monkeypatch.setattr("apps.notifications.eligibility.eligible_recipients", broken)
        with pytest.raises(DatabaseError), transaction.atomic():
            claim_next(system)
        assert reload(message).status == Status.QUEUED

    def test_a_row_taken_meanwhile_is_skipped(
        self,
        system: SystemPrincipal,
        security_mail: Callable[..., EmailOutbox],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        security_mail()
        real_due = services._due
        calls = iter([real_due, EmailOutbox.objects.none])
        monkeypatch.setattr(services, "_due", lambda: next(calls)())
        assert claim_next(system) is None

    def test_retries_keep_the_message_id(
        self, system: SystemPrincipal, security_mail: Callable[..., EmailOutbox]
    ) -> None:
        message = security_mail()
        first = claim(system)
        record_outcome(system, first.attempt_id, Outcome.FAILED, error="SMTP 451 at RCPT")
        make_due(message)
        second = claim(system)
        assert second.attempt_number == 2
        assert second.header_message_id == first.header_message_id


class TestDispatchClaim:
    def test_without_a_source_dispatches_wait(
        self, system: SystemPrincipal, dispatch: Callable[..., EmailOutbox]
    ) -> None:
        assert dispatch_source() is None
        dispatch()
        assert claim_next(system) is None

    def test_the_order_is_locked_before_the_row(
        self,
        system: SystemPrincipal,
        dispatch: Callable[..., EmailOutbox],
        orders: FakeOrders,
        org: Any,
    ) -> None:
        message = dispatch()
        claimed = claim(system)
        assert claimed.to == ("orders@supplier.example",)
        assert orders.locked == [org.hospital.pk]
        assert reload(message).status == Status.SENDING

    @pytest.mark.parametrize("refusal", [Status.CANCELLED, Status.SUPERSEDED])
    def test_a_refused_dispatch_is_settled(
        self,
        system: SystemPrincipal,
        dispatch: Callable[..., EmailOutbox],
        orders: FakeOrders,
        org: Any,
        refusal: Status,
    ) -> None:
        message = dispatch()
        orders.refusals[org.hospital.pk] = refusal
        assert claim_next(system) == Settled(message.pk, refusal)

    def test_one_source_at_a_time(self, orders: FakeOrders) -> None:
        with pytest.raises(ValueError, match="already registered"):
            register_dispatch_source(FakeOrders())

    def test_unregistering_twice_is_harmless(self) -> None:
        unregister = register_dispatch_source(FakeOrders())
        unregister()
        unregister()
        assert dispatch_source() is None


class TestOutcome:
    def test_sent_wipes_the_sensitive_body(
        self, system: SystemPrincipal, security_mail: Callable[..., EmailOutbox]
    ) -> None:
        message = security_mail()
        claimed = claim(system)
        assert record_outcome(system, claimed.attempt_id, Outcome.SENT) is True
        message = reload(message)
        assert (message.status, message.body, message.html_body) == (Status.SENT, "", "")
        assert message.sent_at is not None and message.wiped_at is not None
        attempt = EmailAttempt.objects.get(pk=claimed.attempt_id)
        assert attempt.outcome == Outcome.SENT and attempt.finished_at is not None

    def test_failed_is_retried_with_backoff_then_given_up(
        self, system: SystemPrincipal, notification: Callable[..., EmailOutbox], manager: User
    ) -> None:
        message = notification(manager)
        for number in range(1, 6):
            claimed = claim(system)
            assert claimed.attempt_number == number
            record_outcome(system, claimed.attempt_id, Outcome.FAILED, error="SMTP 451 at RCPT")
            message = reload(message)
            if number < 5:
                assert message.status == Status.QUEUED
                assert claim_next(system) is None  # not before the backoff
                make_due(message)
        assert message.status == Status.FAILED
        assert message.last_error == "SMTP 451 at RCPT"

    def test_a_notification_is_resent_once_after_ambiguous(
        self, system: SystemPrincipal, notification: Callable[..., EmailOutbox], manager: User
    ) -> None:
        message = notification(manager)
        record_outcome(system, claim(system).attempt_id, Outcome.AMBIGUOUS, error="timeout")
        assert reload(message).status == Status.QUEUED
        make_due(message)
        record_outcome(system, claim(system).attempt_id, Outcome.AMBIGUOUS, error="timeout")
        assert reload(message).status == Status.AMBIGUOUS

    def test_a_security_email_is_not_resent_after_ambiguous(
        self, system: SystemPrincipal, security_mail: Callable[..., EmailOutbox]
    ) -> None:
        message = security_mail()
        record_outcome(system, claim(system).attempt_id, Outcome.AMBIGUOUS, error="timeout")
        message = reload(message)
        assert (message.status, message.body) == (Status.AMBIGUOUS, "")

    def test_a_security_email_past_its_hour_is_not_retried(
        self,
        system: SystemPrincipal,
        security_mail: Callable[..., EmailOutbox],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        message = security_mail()
        claimed = claim(system)
        # The expiry is frozen content; let the hour pass instead.
        monkeypatch.setattr(services, "_expired", lambda message: True)
        record_outcome(system, claimed.attempt_id, Outcome.FAILED, error="SMTP 451 at RCPT")
        message = reload(message)
        assert (message.status, message.body) == (Status.FAILED, "")

    def test_expiry_is_read_at_the_database_clock(
        self, security_mail: Callable[..., EmailOutbox], dispatch: Callable[..., EmailOutbox]
    ) -> None:
        assert services._expired(security_mail()) is False
        assert services._expired(dispatch()) is False
        with override_settings(OUTBOX_SENSITIVE_TTL=0):
            assert services._expired(security_mail()) is True

    def test_dispatch_outcomes_reach_the_source(
        self,
        system: SystemPrincipal,
        dispatch: Callable[..., EmailOutbox],
        orders: FakeOrders,
    ) -> None:
        failed, ambiguous, sent = dispatch(), dispatch(), dispatch()
        record_outcome(system, claim(system).attempt_id, Outcome.FAILED, error="refused")
        record_outcome(system, claim(system).attempt_id, Outcome.AMBIGUOUS, error="timeout")
        record_outcome(system, claim(system).attempt_id, Outcome.SENT)
        # Never resent after AMBIGUOUS (M5.3); the cancellation obligation sees SENT and
        # AMBIGUOUS, not a FAILED attempt that will be retried.
        assert reload(failed).status == Status.QUEUED
        assert orders.outcomes == [(ambiguous.pk, Status.AMBIGUOUS), (sent.pk, Status.SENT)]

    def test_a_dispatch_outcome_needs_the_source(
        self, system: SystemPrincipal, dispatch: Callable[..., EmailOutbox]
    ) -> None:
        dispatch()
        unregister = register_dispatch_source(FakeOrders())
        claimed = claim(system)
        unregister()
        with pytest.raises(RuntimeError, match="dispatch source"):
            record_outcome(system, claimed.attempt_id, Outcome.SENT)

    def test_an_attempt_reports_once(
        self, system: SystemPrincipal, security_mail: Callable[..., EmailOutbox]
    ) -> None:
        message = security_mail()
        claimed = claim(system)
        assert record_outcome(system, claimed.attempt_id, Outcome.FAILED, error="x") is True
        assert record_outcome(system, claimed.attempt_id, Outcome.SENT) is False
        assert reload(message).status == Status.QUEUED

    def test_an_unknown_attempt_changes_nothing(self, system: SystemPrincipal) -> None:
        assert record_outcome(system, uuid.uuid7(), Outcome.SENT) is False

    def test_a_late_outcome_never_overwrites_a_newer_attempt(
        self, system: SystemPrincipal, notification: Callable[..., EmailOutbox], manager: User
    ) -> None:
        message = notification(manager)
        first = claim(system)
        age(first.attempt_id, 91)
        assert reconcile_abandoned(system).abandoned == 1
        assert reload(message).status == Status.QUEUED  # notifications retry AMBIGUOUS once
        make_due(message)
        second = claim(system)
        assert record_outcome(system, first.attempt_id, Outcome.SENT) is False
        message = reload(message)
        assert (message.status, message.current_attempt_id) == (Status.SENDING, second.attempt_id)
        assert EmailAttempt.objects.get(pk=first.attempt_id).outcome == Outcome.SENT

    def test_a_late_outcome_of_the_abandoned_attempt_still_counts(
        self, system: SystemPrincipal, security_mail: Callable[..., EmailOutbox]
    ) -> None:
        message = security_mail()
        claimed = claim(system)
        age(claimed.attempt_id, 91)
        reconcile_abandoned(system)
        assert reload(message).status == Status.AMBIGUOUS
        assert record_outcome(system, claimed.attempt_id, Outcome.SENT) is True
        assert reload(message).status == Status.SENT
        attempt = EmailAttempt.objects.get(pk=claimed.attempt_id)
        assert (attempt.outcome, attempt.abandoned) == (Outcome.SENT, False)

    def test_a_status_set_meanwhile_is_kept(
        self, system: SystemPrincipal, security_mail: Callable[..., EmailOutbox]
    ) -> None:
        message = security_mail()
        claimed = claim(system)
        EmailOutbox.objects.filter(pk=message.pk).update(status=Status.QUARANTINED)
        assert record_outcome(system, claimed.attempt_id, Outcome.SENT) is False
        assert reload(message).status == Status.QUARANTINED

    def test_lock_message_of_a_missing_row(self) -> None:
        with transaction.atomic():
            assert services._lock_message(LockOrder(), uuid.uuid7()) is None


class TestAbandoned:
    def test_an_attempt_without_outcome_becomes_ambiguous_after_deadline_and_grace(
        self, system: SystemPrincipal, security_mail: Callable[..., EmailOutbox]
    ) -> None:
        message = security_mail()
        claimed = claim(system)
        age(claimed.attempt_id, 89)
        result = reconcile_abandoned(system)
        assert result.abandoned == 0
        assert result.next_due_in is not None and 0 < result.next_due_in <= 1.5
        age(claimed.attempt_id, 91)
        assert reconcile_abandoned(system) == services.Reconciled(abandoned=1, next_due_in=None)
        message = reload(message)
        assert (message.status, message.body) == (Status.AMBIGUOUS, "")
        attempt = EmailAttempt.objects.get(pk=claimed.attempt_id)
        assert (attempt.outcome, attempt.abandoned) == (Outcome.AMBIGUOUS, True)
        assert attempt.error == services.ABANDONED_ERROR

    def test_an_abandoned_dispatch_triggers_the_obligation(
        self,
        system: SystemPrincipal,
        dispatch: Callable[..., EmailOutbox],
        orders: FakeOrders,
    ) -> None:
        message = dispatch()
        claimed = claim(system)
        age(claimed.attempt_id, 120)
        reconcile_abandoned(system)
        assert orders.outcomes == [(message.pk, Status.AMBIGUOUS)]

    def test_nothing_in_flight(self, system: SystemPrincipal) -> None:
        assert reconcile_abandoned(system) == services.Reconciled(abandoned=0, next_due_in=None)

    def test_a_row_that_changed_since_discovery_is_left_alone(
        self,
        system: SystemPrincipal,
        security_mail: Callable[..., EmailOutbox],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        message = security_mail()
        claimed = claim(system)
        age(claimed.attempt_id, 91)
        monkeypatch.setattr(services, "_abandon_in_txn", lambda *args: False)
        assert reconcile_abandoned(system).abandoned == 0
        assert reload(message).status == Status.SENDING

    def test_abandon_verifies_under_the_lock(
        self, system: SystemPrincipal, security_mail: Callable[..., EmailOutbox]
    ) -> None:
        message = security_mail()
        claimed = claim(system)
        with transaction.atomic():
            # Missing row, wrong attempt, attempt too young.
            assert not services._abandon(system, uuid.uuid7(), claimed.attempt_id)
            assert not services._abandon(system, message.pk, uuid.uuid7())
            assert not services._abandon(system, message.pk, claimed.attempt_id)
        record_outcome(system, claimed.attempt_id, Outcome.SENT)
        with transaction.atomic():
            assert not services._abandon(system, message.pk, claimed.attempt_id)


class TestHousekeeping:
    def test_every_task_runs_with_its_own_capability(
        self,
        system: SystemPrincipal,
        security_mail: Callable[..., EmailOutbox],
        account: User,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        with override_settings(OUTBOX_SENSITIVE_TTL=0):
            queued = security_mail()
            ambiguous = security_mail()
            sending = security_mail()
        EmailOutbox.objects.filter(pk=ambiguous.pk).update(status=Status.AMBIGUOUS)
        attempt = EmailAttempt.objects.create(message=sending, number=1)
        EmailOutbox.objects.filter(pk=sending.pk).update(
            status=Status.SENDING, current_attempt=attempt
        )
        IdempotencyRecord.objects.create(
            actor=account, key_id=uuid.uuid4(), operation="x", payload_hash="0" * 64
        )
        IdempotencyRecord.objects.update(created_at=timezone.now() - timedelta(days=8))
        cache.set("old", 1, timeout=1)
        cache._rows().update(expires=timezone.now() - timedelta(seconds=1))  # type: ignore[attr-defined]
        authorized: list[str] = []
        from apps.access.authorization import authorize_in_txn as real

        def spy(principal: Any, capability: str, **kwargs: Any) -> Any:
            authorized.append(capability)
            return real(principal, capability, **kwargs)

        monkeypatch.setattr("apps.notifications.services.authorize_in_txn", spy)
        done = run_housekeeping(system)
        assert done == {"idempotency": 1, "security_cache": 1, "sensitive_bodies": 2}
        assert authorized == [f"housekeeping.{name}" for name in done]
        assert (reload(queued).status, reload(queued).body) == (Status.EXPIRED, "")
        assert (reload(ambiguous).status, reload(ambiguous).body) == (Status.AMBIGUOUS, "")
        assert reload(sending).body != ""  # left to its outcome

    def test_a_task_needs_its_capability(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(housekeeping._TASKS, "nothing", lambda: 0)
        monkeypatch.setattr(SystemPrincipal, "CAPABILITY_PREFIX", "other.")
        with pytest.raises(AccessDenied):
            run_housekeeping(SystemPrincipal())

    def test_task_names(self) -> None:
        for name in ("", "a.b"):
            with pytest.raises(ValueError, match="Invalid"):
                housekeeping.register_task(name, lambda: 0)
        housekeeping.register_task("idempotency", housekeeping._TASKS["idempotency"])
        with pytest.raises(ValueError, match="already registered"):
            housekeeping.register_task("idempotency", lambda: 0)

    def test_a_cache_without_purge(self) -> None:
        caches = {"default": object()}
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(housekeeping, "caches", caches)
            assert housekeeping.purge_security_cache() == 0
