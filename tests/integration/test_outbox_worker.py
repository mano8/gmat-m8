"""The outbox under real concurrency and a real crash (plan §5, §4.7 F55).

- several workers claiming at once never send one message twice;
- a dispatch claim takes its order before the row (L4 → L4b), so it waits for a cancellation
  that holds the order and then sees its result, and the cancellation waits for a claim in
  progress; neither order deadlocks;
- a worker killed after DATA leaves its message ``SENDING``; a worker started right after
  turns it ``AMBIGUOUS`` once the deadline and grace have passed, and resends nothing.
"""

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from django.db import transaction
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
from apps.purchasing.models import PurchaseOrder
from tests import orders as purchase_orders
from tests.integration.conftest import run_concurrently
from tests.org import build_org
from tests.outbox import SECRET_BODY, FakeOrders, no_dispatch_source, start_worker
from tests.smtp import Behaviour, fake_smtp

pytestmark = [pytest.mark.integration, pytest.mark.serial, pytest.mark.django_db(transaction=True)]


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
    with no_dispatch_source():
        unregister = register_dispatch_source(source)
        yield source
        unregister()


@pytest.fixture
def order_and_dispatch(db: None) -> tuple[PurchaseOrder, EmailOutbox]:
    order = purchase_orders.order(build_org())
    with transaction.atomic():
        message = queue_dispatch(
            kind=Kind.PO_ORDER,
            po_id=order.pk,
            address="orders@supplier.example",
            subject="Order",
            body="Please deliver.",
        )
    return order, message


def _cancel(order_id: Any, message_id: Any, locked: threading.Event, hold: float) -> str:
    """What M5's cancel does: the order FOR UPDATE (L4), then its dispatch rows (L4b)."""
    with transaction.atomic():
        locks = LockOrder()
        locks.lock(LockLevel.PO_HEADERS, PurchaseOrder, [order_id], mode=LockMode.UPDATE)
        locks.lock(LockLevel.PO_OUTBOX, EmailOutbox, [message_id], mode=LockMode.UPDATE)
        locked.set()
        time.sleep(hold)
        status = EmailOutbox.objects.get(pk=message_id).status
        if status == Status.QUEUED:
            EmailOutbox.objects.filter(pk=message_id).update(status=Status.CANCELLED)
    return str(status)


def test_a_dispatch_claim_waits_for_a_cancellation_holding_the_order(
    orders: SlowOrders, order_and_dispatch: tuple[PurchaseOrder, EmailOutbox]
) -> None:
    order, message = order_and_dispatch
    locked = threading.Event()

    def run(index: int) -> Any:
        if index == 0:
            return _cancel(order.pk, message.pk, locked, hold=0.5)
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
    orders: SlowOrders, order_and_dispatch: tuple[PurchaseOrder, EmailOutbox]
) -> None:
    order, message = order_and_dispatch
    orders.hold = 0.5
    locked = threading.Event()

    def run(index: int) -> Any:
        if index == 0:
            return claim_next(SystemPrincipal())
        time.sleep(0.2)  # the claim holds the order FOR SHARE by now
        return _cancel(order.pk, message.pk, locked, hold=0)

    claimed, seen_by_cancel = run_concurrently(2, run)
    assert isinstance(claimed, Claim)
    # The cancellation saw the committed claim: M5 sets cancellation_notice_required then.
    assert seen_by_cancel == Status.SENDING


def test_a_worker_killed_after_data_leaves_ambiguous_and_never_resends(
    make_user: Callable[..., User], tmp_path: Path
) -> None:
    user = make_user("crash@example.org")
    (message,) = security_mails(user, 1)
    # The server takes the whole message, then never answers: the worker is past DATA.
    with fake_smtp(Behaviour(hang_at="final")) as server:
        process = start_worker(server.port, tmp_path)
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
