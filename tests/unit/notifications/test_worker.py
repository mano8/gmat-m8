"""The worker process: staged SMTP outcomes, the deadline, switches and the loop (plan §5)."""

import json
import signal
import socket
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from django.core import mail
from django.core.mail.backends.smtp import EmailBackend as SMTPBackend
from django.core.management import call_command
from django.db import DatabaseError
from django.test import Client, override_settings
from django.urls import reverse

from apps.access.principals import SystemPrincipal
from apps.accounts.models import User
from apps.notifications import worker as worker_module
from apps.notifications.models import EmailOutbox, Outcome, Status
from apps.notifications.services import Claim
from apps.notifications.worker import (
    OUTBOUND_OFF,
    PAUSE_FLAG,
    PAUSED,
    RUNNING,
    Deadline,
    DeadlineExceeded,
    Result,
    Worker,
    build_message,
    send,
    send_deadline,
)
from tests.smtp import Behaviour, fake_smtp, smtp_mailers

pytestmark = pytest.mark.django_db


def a_claim(**overrides: Any) -> Claim:
    values: dict[str, Any] = {
        "message_id": uuid.uuid7(),
        "attempt_id": uuid.uuid7(),
        "attempt_number": 1,
        "kind": "SECURITY",
        "from_email": "gmat-m8 <no-reply@localhost>",
        "to": ("someone@example.org",),
        "bcc": (),
        "subject": "Password reset",
        "header_message_id": "<1@localhost>",
        "body": "the body",
        "html_body": "",
    }
    return Claim(**(values | overrides))


def smtp(port: int, timeout: float = 2, **options: Any) -> SMTPBackend:
    return SMTPBackend(alias="outbound", host="127.0.0.1", port=port, timeout=timeout, **options)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class TestMessage:
    def test_one_recipient_is_addressed_directly(self) -> None:
        message = build_message(a_claim())
        assert message.to == ["someone@example.org"]
        assert message.bcc == []
        rendered = message.message()
        assert rendered["Message-ID"] == "<1@localhost>"
        assert rendered["To"] == "someone@example.org"

    def test_several_recipients_never_see_each_other(self) -> None:
        message = build_message(
            a_claim(to=(), bcc=("a@example.org", "b@example.org"), html_body="<p>hi</p>")
        )
        rendered = message.message()
        assert rendered["To"] == "undisclosed-recipients:;"
        assert "a@example.org" not in rendered.as_string()
        assert message.recipients() == ["a@example.org", "b@example.org"]
        assert message.alternatives[0][1] == "text/html"


class TestSmtpOutcome:
    def test_sent(self) -> None:
        with fake_smtp() as server:
            result = send(a_claim(), mailer=smtp(server.port))
        assert result == Result(Outcome.SENT)
        (received,) = server.received
        assert received.recipients == ["someone@example.org"]
        assert b"Subject: Password reset" in received.content

    def test_some_recipients_refused(self) -> None:
        behaviour = Behaviour(refuse=frozenset({"b@example.org"}))
        with fake_smtp(behaviour) as server:
            claim = a_claim(to=(), bcc=("a@example.org", "b@example.org"))
            result = send(claim, mailer=smtp(server.port))
        assert result == Result(Outcome.SENT, "1 recipient(s) refused")

    @pytest.mark.parametrize(
        ("behaviour", "error"),
        [
            (Behaviour(codes={"greeting": 554}), "SMTP 554 at connect"),
            (Behaviour(codes={"EHLO": 501}), "at MAIL"),
            (Behaviour(codes={"MAIL": 550}), "SMTP 550 at MAIL"),
            (Behaviour(codes={"RCPT": 550}), "SMTP 550 at RCPT"),
            (Behaviour(codes={"DATA": 554}), "SMTP 554 at DATA"),
            (Behaviour(codes={"final": 554}), "SMTP 554 after DATA"),
            (Behaviour(drop_at="MAIL"), "SMTPServerDisconnected at MAIL"),
            # smtplib reports a read timeout as a disconnect.
            (Behaviour(hang_at="RCPT"), "SMTPServerDisconnected at RCPT"),
        ],
    )
    def test_a_definite_rejection_or_an_error_before_data_is_failed(
        self, behaviour: Behaviour, error: str
    ) -> None:
        with fake_smtp(behaviour) as server:
            result = send(a_claim(), mailer=smtp(server.port, timeout=0.5))
        assert result.outcome == Outcome.FAILED
        assert error in result.error
        assert server.received == []

    @pytest.mark.parametrize(
        ("behaviour", "error"),
        [
            (Behaviour(drop_at="final"), "SMTPServerDisconnected after DATA started"),
            (Behaviour(hang_at="final"), "SMTPServerDisconnected after DATA started"),
            (Behaviour(hang_at="DATA"), "SMTPServerDisconnected after DATA started"),
        ],
    )
    def test_a_disconnect_or_timeout_after_data_is_ambiguous(
        self, behaviour: Behaviour, error: str
    ) -> None:
        with fake_smtp(behaviour) as server:
            result = send(a_claim(), mailer=smtp(server.port, timeout=0.5))
        assert result == Result(Outcome.AMBIGUOUS, error)

    def test_a_refused_connection_is_failed(self) -> None:
        result = send(a_claim(), mailer=smtp(free_port()))
        assert result == Result(Outcome.FAILED, "ConnectionRefusedError at connect")

    def test_starttls_and_auth_the_server_lacks(self) -> None:
        with fake_smtp() as server:
            tls = send(a_claim(), mailer=smtp(server.port, use_tls=True))
            auth = send(a_claim(), mailer=smtp(server.port, username="u", password="p"))
        assert tls == Result(Outcome.FAILED, "SMTPNotSupportedError at STARTTLS")
        assert auth == Result(Outcome.FAILED, "SMTPNotSupportedError at AUTH")

    def test_an_invalid_address_is_failed_before_connecting(self) -> None:
        with fake_smtp() as server:
            result = send(a_claim(to=("not an address\n",)), mailer=smtp(server.port))
        assert result == Result(Outcome.FAILED, "ValueError at prepare")
        assert server.connections == 0

    def test_the_server_text_is_never_recorded(self) -> None:
        with fake_smtp(Behaviour(codes={"RCPT": 550})) as server:
            result = send(a_claim(), mailer=smtp(server.port))
        assert "someone" not in result.error
        assert "fake" not in result.error


class TestDeadline:
    def test_a_server_slow_at_every_stage_cannot_hold_a_message_past_the_deadline(self) -> None:
        # Each reply trickles one byte every 0.2 s: every read returns well within the
        # per-operation timeout, so only the total deadline (SIGALRM) stops it.
        behaviour = Behaviour(delay=0.2, trickle=True)
        with fake_smtp(behaviour) as server, override_settings(OUTBOX_SEND_DEADLINE=1):
            started = time.monotonic()
            result = send(a_claim(), mailer=smtp(server.port, timeout=10))
            elapsed = time.monotonic() - started
        assert elapsed < 1.5
        assert result == Result(Outcome.FAILED, "DeadlineExceeded at connect")

    def test_the_deadline_after_data_is_ambiguous(self) -> None:
        behaviour = Behaviour(delay=0.2, trickle=True, slow_at=frozenset({"final"}))
        with fake_smtp(behaviour) as server, override_settings(OUTBOX_SEND_DEADLINE=1):
            result = send(a_claim(), mailer=smtp(server.port, timeout=10))
        assert result == Result(Outcome.AMBIGUOUS, "DeadlineExceeded after DATA started")

    def test_outside_the_main_thread_each_operation_gets_what_is_left(self) -> None:
        behaviour = Behaviour(delay=0.7, slow_at=frozenset({"EHLO", "MAIL", "RCPT"}))
        results: list[Result] = []
        with fake_smtp(behaviour) as server, override_settings(OUTBOX_SEND_DEADLINE=1):
            started = time.monotonic()
            thread = threading.Thread(
                target=lambda: results.append(send(a_claim(), mailer=smtp(server.port, 10)))
            )
            thread.start()
            thread.join(10)
            elapsed = time.monotonic() - started
        assert elapsed < 1.6
        assert results[0].outcome == Outcome.FAILED

    def test_the_alarm_is_disarmed_and_the_handler_restored(self) -> None:
        before = signal.getsignal(signal.SIGALRM)
        with send_deadline(5) as deadline:
            assert signal.getitimer(signal.ITIMER_REAL)[0] > 0
            assert deadline.op_timeout(10) <= 5
            assert deadline.op_timeout(1) == 1
        assert signal.getitimer(signal.ITIMER_REAL)[0] == 0
        assert signal.getsignal(signal.SIGALRM) is before

    def test_no_time_left(self) -> None:
        with pytest.raises(DeadlineExceeded):
            Deadline(time.monotonic() - 1).op_timeout(10)

    def test_a_done_send_ignores_a_late_alarm(self) -> None:
        with send_deadline(0.05) as deadline:
            deadline.done = True
            time.sleep(0.1)  # the alarm fires and is ignored

    def test_an_alarm_outside_the_dialogue_is_ambiguous(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def late(*args: Any) -> Result:
            raise DeadlineExceeded

        monkeypatch.setattr(worker_module, "_smtp_send", late)
        assert send(a_claim(), mailer=smtp(1)) == Result(Outcome.AMBIGUOUS, "DeadlineExceeded")


class TestOtherMailers:
    def test_locmem_delivers(self) -> None:
        assert send(a_claim()) == Result(Outcome.SENT)
        assert [message.subject for message in mail.outbox] == ["Password reset"]

    def test_a_refusal_is_failed_and_an_error_ambiguous(self) -> None:
        class Refusing:
            def send_messages(self, messages: list[Any]) -> int:
                return 0

        class Broken:
            def send_messages(self, messages: list[Any]) -> int:
                raise OSError("down")

        assert send(a_claim(), mailer=Refusing()) == Result(Outcome.FAILED, "not accepted")
        assert send(a_claim(), mailer=Broken()) == Result(Outcome.AMBIGUOUS, "OSError in Broken")


@pytest.fixture
def flags(tmp_path: Path) -> Path:
    path = tmp_path / "flags"
    path.mkdir()
    return path


@pytest.fixture
def make_worker(flags: Path, tmp_path: Path, system: SystemPrincipal) -> Callable[..., Worker]:
    def make(**overrides: Any) -> Worker:
        values: dict[str, Any] = {
            "principal": system,
            "flags_dir": flags,
            "heartbeat_file": tmp_path / "heartbeat",
            "outbound": True,
            "poll_interval": 0.01,
            "reconcile_interval": 60,
            "housekeeping_interval": 3600,
            "name": "test-worker",
        }
        return Worker(**(values | overrides))

    return make


def heartbeat(worker: Worker) -> dict[str, Any]:
    return dict(json.loads(worker.heartbeat_file.read_text(encoding="utf-8")))


class TestSwitches:
    def test_paused_claims_nothing_and_touches_no_database(
        self,
        make_worker: Callable[..., Worker],
        flags: Path,
        security_mail: Callable[..., EmailOutbox],
        django_assert_num_queries: Any,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        message = security_mail()
        closed: list[bool] = []
        monkeypatch.setattr(
            "apps.notifications.worker.connections.close_all", lambda: closed.append(True)
        )
        (flags / PAUSE_FLAG).touch()
        worker = make_worker()
        with django_assert_num_queries(0):
            assert worker.tick() is False
            assert worker.tick() is False
        assert heartbeat(worker)["state"] == PAUSED
        assert caplog.text.count("Worker paused") == 1
        assert closed == [True]  # a deploy may migrate meanwhile
        assert EmailOutbox.objects.get(pk=message.pk).status == Status.QUEUED
        (flags / PAUSE_FLAG).unlink()
        assert worker.tick() is True
        assert "Worker resumed" in caplog.text
        assert heartbeat(worker)["state"] == RUNNING

    def test_outbound_off_claims_nothing_but_keeps_the_database_tidy(
        self,
        make_worker: Callable[..., Worker],
        security_mail: Callable[..., EmailOutbox],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        message = security_mail()
        ran: list[str] = []

        def housekeeping(principal: Any) -> dict[str, int]:
            ran.append("hk")
            return {}

        monkeypatch.setattr(worker_module, "run_housekeeping", housekeeping)
        worker = make_worker(outbound=False, send=lambda claim: pytest.fail("sent"))
        assert worker.tick() is False
        assert ran == ["hk"]
        assert heartbeat(worker)["state"] == OUTBOUND_OFF
        assert EmailOutbox.objects.get(pk=message.pk).status == Status.QUEUED
        assert mail.outbox == []


class TestTick:
    def test_claims_sends_and_records(
        self,
        make_worker: Callable[..., Worker],
        security_mail: Callable[..., EmailOutbox],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        message = security_mail()
        worker = make_worker()
        assert worker.tick() is True
        assert worker.tick() is False  # nothing left
        message.refresh_from_db()
        assert message.status == Status.SENT
        assert [sent.to for sent in mail.outbox] == [["reset-me@example.org"]]
        assert "reset-me" not in caplog.text
        assert "SECRETTOKEN" not in caplog.text

    def test_a_settled_message_is_followed_by_the_next(
        self,
        make_worker: Callable[..., Worker],
        security_mail: Callable[..., EmailOutbox],
        account: User,
    ) -> None:
        security_mail()
        User.objects.filter(pk=account.pk).update(is_active=False)
        assert make_worker().tick() is True
        assert EmailOutbox.objects.get().status == Status.SUPPRESSED

    def test_an_outcome_superseded_meanwhile_is_logged(
        self,
        make_worker: Callable[..., Worker],
        security_mail: Callable[..., EmailOutbox],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        message = security_mail()

        def quarantined_while_sending(claim: Claim) -> Result:
            EmailOutbox.objects.filter(pk=claim.message_id).update(status=Status.QUARANTINED)
            return Result(Outcome.SENT)

        make_worker(send=quarantined_while_sending).tick()
        assert "superseded, not applied" in caplog.text
        assert EmailOutbox.objects.get(pk=message.pk).status == Status.QUARANTINED

    def test_reconciliation_and_housekeeping_are_scheduled(
        self, make_worker: Callable[..., Worker], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from apps.notifications.services import Reconciled

        now = [1000.0]
        due: list[float | None] = [30.0]
        calls: list[str] = []

        def reconcile(principal: Any) -> Reconciled:
            calls.append("reconcile")
            return Reconciled(abandoned=1, next_due_in=due[0])

        def housekeeping(principal: Any) -> dict[str, int]:
            calls.append("hk")
            return {}

        monkeypatch.setattr(worker_module, "reconcile_abandoned", reconcile)
        monkeypatch.setattr(worker_module, "run_housekeeping", housekeeping)
        worker = make_worker(clock=lambda: now[0], outbound=False)
        worker.tick()
        assert calls == ["reconcile", "hk"]
        # The oldest attempt in flight is abandoned in 30 s: look again then, not in 60 s.
        assert worker.next_reconcile == 1030.5
        assert worker.next_housekeeping == 4600.0
        now[0], due[0] = 1031.0, None
        worker.tick()
        assert worker.next_reconcile == 1091.0
        now[0] = 4600.0
        worker.tick()
        assert calls == ["reconcile", "hk", "reconcile", "reconcile", "hk"]


class TestRun:
    @pytest.fixture(autouse=True)
    def _keep_signal_handlers(self) -> Iterator[None]:
        saved = {signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGINT)}
        yield
        for signum, handler in saved.items():
            signal.signal(signum, handler)

    def test_runs_until_stopped_and_waits_out_database_outages(
        self, make_worker: Callable[..., Worker], caplog: pytest.LogCaptureFixture
    ) -> None:
        worker = make_worker()
        ticks = iter([DatabaseError("down"), True, "stop"])

        def tick() -> bool:
            step = next(ticks)
            if isinstance(step, Exception):
                raise step
            if step == "stop":
                signal.raise_signal(signal.SIGTERM)  # the handler run() installed
                return False
            return bool(step)

        worker.tick = tick  # type: ignore[method-assign]
        worker.run()
        assert worker.stop.is_set()
        assert "Database unavailable" in caplog.text
        assert "Worker stopped" in caplog.text

    def test_from_settings(self, settings: Any, tmp_path: Path) -> None:
        settings.WORKER_FLAGS_DIR = str(tmp_path)
        settings.OUTBOUND_EMAIL = False
        worker = Worker.from_settings()
        assert worker.principal == SystemPrincipal()
        assert worker.flags_dir == tmp_path
        assert worker.outbound is False
        assert worker.poll_interval == 2
        assert worker.name.endswith(f":{__import__('os').getpid()}")

    def test_the_command_runs_the_worker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ran: list[Worker] = []
        monkeypatch.setattr(Worker, "run", lambda self: ran.append(self))
        call_command("worker")
        assert len(ran) == 1


class TestNoSmtpInRequests:
    def test_request_latency_does_not_depend_on_smtp(
        self, client: Client, make_worker: Callable[..., Worker], account: User
    ) -> None:
        # A server that answers slowly at every stage: a request that waited for it would
        # take seconds. The request only writes the outbox row.
        behaviour = Behaviour(delay=0.3, trickle=True)
        with (
            fake_smtp(behaviour) as server,
            override_settings(MAILERS=smtp_mailers(server.port), OUTBOX_SEND_DEADLINE=1),
        ):
            started = time.monotonic()
            response = client.post(reverse("account_reset_password"), {"email": account.email})
            elapsed = time.monotonic() - started
            assert response.status_code == 302
            assert elapsed < 1
            assert server.connections == 0
            assert EmailOutbox.objects.get().status == Status.QUEUED
            # The worker meets the slow server, within its deadline.
            started = time.monotonic()
            make_worker(send=send).tick()
            assert time.monotonic() - started < 2
        message = EmailOutbox.objects.get()
        assert message.status == Status.QUEUED  # FAILED before DATA: retried later
        assert message.current_attempt.outcome == Outcome.FAILED  # type: ignore[union-attr]
