"""The outbox under real concurrency and a real crash (plan §5, §4.7 F55).

- several workers claiming at once never send one message twice;
- a dispatch claim takes its order before the row (L4 → L4b), so it waits for a cancellation
  that holds the order and then sees its result, and the cancellation waits for a claim in
  progress; neither order deadlocks;
- a worker killed after DATA leaves its message ``SENDING``; a worker started right after
  turns it ``AMBIGUOUS`` once the deadline and grace have passed, and resends nothing.
"""

import os
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from django.db import connection, transaction
from django.test import override_settings

from apps.access.principals import SystemPrincipal
from apps.accounts.models import User
from apps.core.locking import LockLevel, LockMode, LockOrder
from apps.notifications.dispatch import register_dispatch_source
from apps.notifications.eligibility import ACCOUNT_ACTIVE
from apps.notifications.models import EmailAttempt, EmailOutbox, Kind, Outcome, Status
from apps.notifications.outbox import queue_dispatch, queue_security_email
from apps.notifications.services import Claim, claim_next, record_outcome
from apps.notifications.worker import Worker
from apps.projects.models import Project
from tests.integration.conftest import run_concurrently
from tests.org import Org, build_org
from tests.outbox import SECRET_BODY, FakeOrders
from tests.smtp import Behaviour, fake_smtp

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]

ROOT = Path(__file__).resolve().parents[2]


def security_mails(user: User, count: int) -> list[EmailOutbox]:
    return [
        queue_security_email(
            address=user.email, target=user, rule=ACCOUNT_ACTIVE, subject="s", body=SECRET_BODY
        )
        for _ in range(count)
    ]


def test_two_workers_never_send_the_same_message(make_user: Callable[..., User]) -> None:
    user = make_user("many@example.org")
    messages = security_mails(user, 24)
    sent: list[Any] = []
    guard = threading.Lock()

    def work(index: int) -> int:
        system = SystemPrincipal()
        while EmailOutbox.objects.filter(status=Status.QUEUED).exists():
            claimed = claim_next(system, worker=f"w{index}")
            if isinstance(claimed, Claim):
                with guard:
                    sent.append(claimed.message_id)
                record_outcome(system, claimed.attempt_id, Outcome.SENT)
        return 0

    assert run_concurrently(4, work) == [0, 0, 0, 0]
    assert sorted(sent) == sorted(message.pk for message in messages)
    assert set(EmailOutbox.objects.values_list("status", flat=True)) == {Status.SENT}
    assert EmailAttempt.objects.count() == len(messages)


class SlowOrders(FakeOrders):
    """Holds the claim's locks a while, so another transaction can collide with it."""

    hold = 0.0

    def refusal(self, message: EmailOutbox) -> Status | None:
        time.sleep(self.hold)
        return super().refusal(message)


@pytest.fixture
def orders() -> Any:
    source = SlowOrders()
    unregister = register_dispatch_source(source)
    yield source
    unregister()


@pytest.fixture
def order_and_dispatch(db: None) -> tuple[Org, EmailOutbox]:
    org = build_org()
    with transaction.atomic():
        message = queue_dispatch(
            kind=Kind.PO_ORDER,
            po_id=org.hospital.pk,
            address="orders@supplier.example",
            subject="Order",
            body="Please deliver.",
        )
    return org, message


def _cancel(order_id: Any, message_id: Any, locked: threading.Event, hold: float) -> str:
    """What M5's cancel does: the order FOR UPDATE (L4), then its dispatch rows (L4b)."""
    with transaction.atomic():
        locks = LockOrder()
        locks.lock(LockLevel.PO_HEADERS, Project, [order_id], mode=LockMode.UPDATE)
        locks.lock(LockLevel.PO_OUTBOX, EmailOutbox, [message_id], mode=LockMode.UPDATE)
        locked.set()
        time.sleep(hold)
        status = EmailOutbox.objects.get(pk=message_id).status
        if status == Status.QUEUED:
            EmailOutbox.objects.filter(pk=message_id).update(status=Status.CANCELLED)
    return str(status)


def test_a_dispatch_claim_waits_for_a_cancellation_holding_the_order(
    orders: SlowOrders, order_and_dispatch: tuple[Org, EmailOutbox]
) -> None:
    org, message = order_and_dispatch
    locked = threading.Event()

    def run(index: int) -> Any:
        if index == 0:
            return _cancel(org.hospital.pk, message.pk, locked, hold=0.5)
        locked.wait(10)
        started = time.monotonic()
        result = claim_next(SystemPrincipal())
        return result, time.monotonic() - started

    cancelled, (claimed, waited) = run_concurrently(2, run)
    assert cancelled == Status.QUEUED  # the cancellation came first...
    assert claimed is None  # ...so the claim found the row cancelled under its locks
    assert waited >= 0.3  # it waited on the order lock, not on the row
    assert EmailOutbox.objects.get(pk=message.pk).status == Status.CANCELLED
    assert EmailAttempt.objects.count() == 0


def test_a_cancellation_waits_for_a_claim_in_progress(
    orders: SlowOrders, order_and_dispatch: tuple[Org, EmailOutbox]
) -> None:
    org, message = order_and_dispatch
    orders.hold = 0.5
    locked = threading.Event()

    def run(index: int) -> Any:
        if index == 0:
            return claim_next(SystemPrincipal())
        time.sleep(0.2)  # the claim holds the order FOR SHARE by now
        return _cancel(org.hospital.pk, message.pk, locked, hold=0)

    claimed, seen_by_cancel = run_concurrently(2, run)
    assert isinstance(claimed, Claim)
    # The cancellation saw the committed claim: M5 sets cancellation_notice_required then.
    assert seen_by_cancel == Status.SENDING


WORKER_SCRIPT = textwrap.dedent(
    """
    import os, sys
    import django

    django.setup()
    from django.conf import settings

    settings.MAILERS = {
        "default": {"BACKEND": "apps.notifications.backends.OutboxEmailBackend"},
        "outbound": {
            "BACKEND": "django.core.mail.backends.smtp.EmailBackend",
            "OPTIONS": {"host": "127.0.0.1", "port": int(sys.argv[1]), "timeout": 10},
        },
    }
    settings.OUTBOUND_EMAIL = True
    from apps.notifications.worker import main

    main()
    """
)


def test_a_worker_killed_after_data_leaves_ambiguous_and_never_resends(
    make_user: Callable[..., User], tmp_path: Path
) -> None:
    user = make_user("crash@example.org")
    (message,) = security_mails(user, 1)
    env = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "config.settings.test",
        "DB_NAME": str(connection.settings_dict["NAME"]),
        "WORKER_FLAGS_DIR": str(tmp_path),
        "WORKER_HEARTBEAT_FILE": str(tmp_path / "heartbeat"),
        "PYTHONPATH": str(ROOT),
    }
    # The server takes the whole message, then never answers: the worker is past DATA.
    with fake_smtp(Behaviour(hang_at="final")) as server:
        process = subprocess.Popen(  # noqa: S603 (fixed interpreter and script)
            [sys.executable, "-c", WORKER_SCRIPT, str(server.port)],
            cwd=ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            assert server.hanging.wait(60), "the worker never reached the end of DATA"
            assert server.contents, "the message content was not transmitted"
        finally:
            process.kill()  # crash: no outcome is ever recorded
            process.wait(10)
    message.refresh_from_db()
    assert message.status == Status.SENDING
    attempt = EmailAttempt.objects.get(message=message)
    assert attempt.outcome == ""

    # Restarted immediately: start-up reconciliation finds the attempt too young, so the
    # worker looks again exactly when it passes the deadline + grace (here 1 s + 1 s).
    worker = Worker.from_settings(
        flags_dir=tmp_path,
        heartbeat_file=tmp_path / "restarted",
        poll_interval=0.05,
        send=lambda claim: pytest.fail("an AMBIGUOUS security email is never resent"),
    )
    with override_settings(OUTBOX_SEND_DEADLINE=1, OUTBOX_ABANDON_GRACE=1):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            worker.tick()
            message.refresh_from_db()
            if message.status != Status.SENDING:
                break
            time.sleep(0.05)
    assert message.status == Status.AMBIGUOUS
    assert message.body == ""
    attempt.refresh_from_db()
    assert (attempt.outcome, attempt.abandoned) == (Outcome.AMBIGUOUS, True)
    assert attempt.finished_at is not None
    took = (attempt.finished_at - attempt.started_at).total_seconds()
    assert 2 <= took < 3.5  # deadline + grace, plus the 0.5 s scheduling slack and polling
