"""Supplier dispatches under real concurrency, a stalled server and a real crash (M5.3).

The purchasing app's own ``DispatchSource`` is registered (``apps.purchasing.apps``):

- two workers racing on one dispatch send it once;
- a server that stops answering after DATA leaves the dispatch ``AMBIGUOUS``, never resent;
- a worker killed mid-send leaves it ``AMBIGUOUS`` after a restart, audited as abandoned;
- a cancel while sending: the dispatch ends ``SENT``, then exactly one cancellation notice is
  queued and sent;
- two outcomes of one cancelled order racing to queue its notice queue it once;
- the claim / cancel interleaving that used to deadlock (claim holds the order ``FOR
  SHARE``, cancel wants it ``FOR UPDATE``, or the other way round) completes either way.
"""

import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from django.core import mail
from django.core.mail.backends.smtp import EmailBackend as SMTPBackend
from django.db import transaction
from django.test import override_settings

from apps.access.principals import SystemPrincipal
from apps.audit.models import AuditEvent
from apps.core import events
from apps.notifications.models import EmailAttempt, EmailOutbox, Kind, Outcome, Status
from apps.notifications.outbox import queue_dispatch
from apps.notifications.services import Claim, claim_next, record_outcome
from apps.notifications.worker import Result, Worker, send
from apps.purchasing import dispatch, services
from apps.purchasing.dispatch import OrderDispatches, queue_order
from apps.purchasing.models import PurchaseOrder
from tests import orders
from tests.integration.conftest import run_concurrently
from tests.org import Org, People
from tests.outbox import make_dispatch, start_worker
from tests.smtp import Behaviour, fake_smtp
from tests.support import principal_for

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]

HOLD = 0.5  # seconds the first transaction keeps the order locked
HEAD_START = 0.2  # seconds before the second one starts


@pytest.fixture
def issue(org: Org, people: People, settings: Any) -> Callable[[], PurchaseOrder]:
    """An issued order of A-MAD, queued to the sentinel supplier as ``issue`` queues it."""
    settings.PO_DISPATCH = True

    def make() -> PurchaseOrder:
        order = orders.order(
            org,
            status="ISSUED",
            issued_by=people.mm_mad,
            supplier_snapshot={"name": org.supplier.name, "email": org.supplier.email},
        )
        with transaction.atomic():
            queue_order(order)
        return order

    return make


@pytest.fixture
def worker(tmp_path: Path) -> Callable[..., Worker]:
    def make(**overrides: Any) -> Worker:
        values: dict[str, Any] = {
            "principal": SystemPrincipal(),
            "flags_dir": tmp_path,
            "heartbeat_file": tmp_path / "heartbeat",
            "outbound": True,
            "poll_interval": 0.01,
            "reconcile_interval": 60,
            "housekeeping_interval": 3600,
            "name": "test-worker",
        }
        return Worker(**(values | overrides))

    return make


def dispatch_of(order: PurchaseOrder, kind: Kind = Kind.PO_ORDER) -> EmailOutbox:
    return EmailOutbox.objects.get(source_po=order, kind=kind)


def test_two_workers_racing_on_one_dispatch_send_it_once(
    issue: Callable[[], PurchaseOrder],
) -> None:
    order = issue()
    results = run_concurrently(2, lambda index: claim_next(SystemPrincipal(), worker=f"w{index}"))
    assert sorted(type(result).__name__ for result in results) == ["Claim", "NoneType"]
    assert EmailAttempt.objects.filter(message__source_po=order).count() == 1
    assert dispatch_of(order).status == Status.SENDING


def test_workers_draining_many_dispatches_send_each_once(
    issue: Callable[[], PurchaseOrder],
) -> None:
    placed = [issue() for _ in range(8)]
    sent: list[Any] = []

    def work(index: int) -> int:
        system = SystemPrincipal()
        while EmailOutbox.objects.filter(status=Status.QUEUED).exists():
            claimed = claim_next(system, worker=f"w{index}")
            if isinstance(claimed, Claim):
                sent.append(claimed.message_id)
                record_outcome(system, claimed.attempt_id, Outcome.SENT)
        return 0

    assert run_concurrently(4, work) == [0, 0, 0, 0]
    assert sorted(sent) == sorted(dispatch_of(order).pk for order in placed)
    assert EmailAttempt.objects.count() == len(placed)


def test_a_server_that_stops_answering_after_data_leaves_it_ambiguous(
    issue: Callable[[], PurchaseOrder], worker: Callable[..., Worker]
) -> None:
    order = issue()
    with fake_smtp(Behaviour(hang_at="final")) as server:
        mailer = SMTPBackend(alias="outbound", host="127.0.0.1", port=server.port, timeout=0.5)
        running = worker(send=lambda claim: send(claim, mailer=mailer))
        assert running.tick() is True
        assert str(order.number).encode() in server.contents[0]
        assert running.tick() is False  # never resent automatically
    message = dispatch_of(order)
    assert (message.status, message.attempts) == (Status.AMBIGUOUS, 1)
    assert AuditEvent.objects.filter(
        object_id=order.pk, action="purchase_order.dispatch_ambiguous"
    ).exists()


def test_a_worker_killed_mid_send_leaves_it_ambiguous_after_a_restart(
    issue: Callable[[], PurchaseOrder], worker: Callable[..., Worker], tmp_path: Path
) -> None:
    order = issue()
    with fake_smtp(Behaviour(hang_at="final")) as server:
        process = start_worker(server.port, tmp_path)
        try:
            assert server.hanging.wait(60), "the worker never reached the end of DATA"
        finally:
            process.kill()  # crash: no outcome is ever recorded
            process.wait(10)
    assert dispatch_of(order).status == Status.SENDING

    restarted = worker(send=lambda claim: pytest.fail("an AMBIGUOUS dispatch is never resent"))
    with override_settings(OUTBOX_SEND_DEADLINE=1, OUTBOX_ABANDON_GRACE=1):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and dispatch_of(order).status == Status.SENDING:
            restarted.tick()
            time.sleep(0.05)
    assert dispatch_of(order).status == Status.AMBIGUOUS
    event = AuditEvent.objects.get(object_id=order.pk, action="purchase_order.dispatch_ambiguous")
    assert (event.actor_label, event.data["abandoned"]) == ("system:worker", True)


def test_a_cancel_while_sending_ends_sent_then_sends_one_notice(
    issue: Callable[[], PurchaseOrder], worker: Callable[..., Worker], people: People
) -> None:
    order = issue()
    buyer = principal_for(people.mm_mad)

    def cancel_meanwhile(claim: Claim) -> Result:
        """The buyer cancels while the worker talks to the supplier's server."""
        services.cancel(buyer, order.pk, reason="Project stopped")
        return send(claim)

    assert worker(send=cancel_meanwhile).tick() is True
    assert PurchaseOrder.objects.get(pk=order.pk).cancellation_notice_required is True
    assert dispatch_of(order).status == Status.SENT
    notice = dispatch_of(order, Kind.PO_CANCELLATION)
    sender = worker()
    assert sender.tick() is True
    assert sender.tick() is False
    assert dispatch_of(order, Kind.PO_CANCELLATION).status == Status.SENT
    assert [message.subject for message in mail.outbox][-1] == notice.subject
    assert EmailOutbox.objects.filter(source_po=order, kind=Kind.PO_CANCELLATION).count() == 1


@pytest.fixture
def slow_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    """The claim holds the order ``FOR SHARE`` (and the row) for ``HOLD``."""
    refusal = OrderDispatches.refusal

    def slow(self: OrderDispatches, message: EmailOutbox) -> Status | None:
        time.sleep(HOLD)
        return refusal(self, message)

    monkeypatch.setattr(OrderDispatches, "refusal", slow)


@pytest.fixture
def slow_cancel() -> Iterator[None]:
    """The cancel holds the order ``FOR UPDATE`` (and its rows) for ``HOLD``."""
    unsubscribe = events.subscribe("purchase_order.cancelled", lambda event: time.sleep(HOLD))
    yield
    unsubscribe()


def second_starts_later(*operations: Callable[[], Any]) -> list[Any]:
    def run(index: int) -> Any:
        if index:
            time.sleep(HEAD_START)
        return operations[index]()

    return run_concurrently(len(operations), run)


def test_a_cancel_waits_for_a_claim_in_progress(
    issue: Callable[[], PurchaseOrder], people: People, slow_claim: None
) -> None:
    order = issue()
    claimed, cancelled = second_starts_later(
        lambda: claim_next(SystemPrincipal()),
        lambda: services.cancel(principal_for(people.mm_mad), order.pk, reason="Stop"),
    )
    assert isinstance(claimed, Claim)
    assert isinstance(cancelled, PurchaseOrder)  # no deadlock, no lock timeout
    # The cancel saw the committed claim: the notice is owed once the outcome is known.
    assert cancelled.cancellation_notice_required is True
    assert dispatch_of(order).status == Status.SENDING
    record_outcome(SystemPrincipal(), claimed.attempt_id, Outcome.SENT)
    assert dispatch_of(order, Kind.PO_CANCELLATION).status == Status.QUEUED


def test_a_claim_waits_for_a_cancel_holding_the_order(
    issue: Callable[[], PurchaseOrder], people: People, slow_cancel: None
) -> None:
    order = issue()
    cancelled, claimed = second_starts_later(
        lambda: services.cancel(principal_for(people.mm_mad), order.pk, reason="Stop"),
        lambda: claim_next(SystemPrincipal()),
    )
    assert isinstance(cancelled, PurchaseOrder)
    assert claimed is None  # it found the dispatch cancelled under its locks
    assert dispatch_of(order).status == Status.CANCELLED
    assert cancelled.cancellation_notice_required is False
    assert EmailAttempt.objects.count() == 0


def test_two_outcomes_racing_to_queue_the_notice_queue_it_once(
    issue: Callable[[], PurchaseOrder], people: People, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both outcome transactions hold the order only FOR SHARE: the second one's insert waits
    on the first's, fails on the unique index and finds the committed notice."""
    order = issue()
    make_dispatch(order)  # a second dispatch of the same revision
    system = SystemPrincipal()
    claims = [claim_next(system), claim_next(system)]
    assert all(isinstance(claimed, Claim) for claimed in claims)
    services.cancel(principal_for(people.mm_mad), order.pk, reason="Stop")
    first: list[EmailOutbox] = []

    def slow_first(**values: Any) -> EmailOutbox:
        queued = queue_dispatch(**values)
        if not first:
            first.append(queued)
            time.sleep(HOLD)  # the notice is inserted, not yet committed
        return queued

    monkeypatch.setattr(dispatch, "queue_dispatch", slow_first)
    results = second_starts_later(
        *(
            lambda claimed=claimed: record_outcome(system, claimed.attempt_id, Outcome.SENT)
            for claimed in claims
        )
    )
    assert results == [True, True]
    assert [message.pk for message in first] == [dispatch_of(order, Kind.PO_CANCELLATION).pk]
