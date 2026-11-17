"""Sending an issued order from its page (M14.4; ADR-0010 Q7 as amended by Q82/Q83).

``PO_DISPATCH`` has three values: ``off`` (print only), ``manual`` (the send from the order's
page only) and ``on`` (also at issue and amendment). ``send_order`` queues the current revision
to the supplier's **current** address, refuses while one of the revision is on its way, gives a
repeat its own ``Message-ID`` and writes nothing when it refuses. The access matrix is
``test_matrix``; the races are in ``tests/integration/test_po_dispatch.py``.
"""

from pathlib import Path
from typing import Any

import pytest
from django.core import mail
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone, translation

from apps.access.context import AccessContext
from apps.access.exceptions import NotInScope
from apps.access.principals import SessionPrincipal, SystemPrincipal
from apps.audit.models import AuditEvent
from apps.core import events
from apps.core.idempotency import IdempotencyConflict, issue_key
from apps.core.models import IdempotencyRecord
from apps.notifications.models import EmailOutbox, Kind, Outcome, Status
from apps.notifications.services import Claim, claim_next, record_outcome
from apps.notifications.worker import Worker
from apps.organizations.models import Membership
from apps.purchasing import dispatch, selectors, services
from apps.purchasing.dispatch import DispatchMode, Queued, queue_order
from apps.purchasing.models import PurchaseOrder
from tests.org import Org, People
from tests.outbox import make_dispatch
from tests.support import error_codes, principal_for
from tests.unit.purchasing.conftest import Buy, MakeLine

pytestmark = pytest.mark.django_db

OrderStatus = PurchaseOrder.Status
NEW_ADDRESS = "new-orders@supplier.example"


@pytest.fixture
def manual(settings: Any) -> None:
    settings.PO_DISPATCH = "manual"


@pytest.fixture
def issued(manual: None, buy: Buy, make_line: MakeLine) -> PurchaseOrder:
    """An issued order of 10 m of cable; ``manual``, so nothing was queued at issue."""
    return buy((make_line("10"), "10"))


@pytest.fixture
def system() -> SystemPrincipal:
    return SystemPrincipal()


def rows(order: PurchaseOrder, kind: Kind = Kind.PO_ORDER) -> list[EmailOutbox]:
    return list(EmailOutbox.objects.filter(source_po=order, kind=kind).order_by("created_at", "id"))


def send(principal: SessionPrincipal, order: PurchaseOrder, **kwargs: Any) -> EmailOutbox:
    kwargs.setdefault("idempotency_key", None)
    return services.send_order(principal, order.pk, **kwargs)


def refused(principal: SessionPrincipal, order: PurchaseOrder) -> set[str]:
    """The refusal's codes; nothing was written (outbox, audit, idempotency records)."""
    before = (
        EmailOutbox.objects.count(),
        AuditEvent.objects.count(),
        IdempotencyRecord.objects.count(),
    )
    key = issue_key(principal.user_id, "purchase_order.send")
    with pytest.raises(ValidationError) as exc:
        send(principal, order, idempotency_key=key)
    after = (
        EmailOutbox.objects.count(),
        AuditEvent.objects.count(),
        IdempotencyRecord.objects.count(),
    )
    assert after == before
    return error_codes(exc)


def claim(system: SystemPrincipal) -> Claim:
    claimed = claim_next(system)
    assert isinstance(claimed, Claim), claimed
    return claimed


# --- the switch -------------------------------------------------------------------------------


class TestMode:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [("off", Queued.OFF), ("manual", Queued.MANUAL), ("on", Queued.QUEUED)],
    )
    def test_queue_order_sends_only_under_on(
        self, settings: Any, buy: Buy, make_line: MakeLine, value: str, expected: Queued
    ) -> None:
        settings.PO_DISPATCH = value
        order = buy((make_line("10"), "10"))
        assert dispatch.mode() == DispatchMode(value)
        assert len(rows(order)) == (1 if value == "on" else 0)
        event = AuditEvent.objects.get(action="purchase_order.issued")
        assert event.data["dispatch"] == expected

    def test_manual_supersedes_an_older_queued_revision_at_amendment(
        self, issued: PurchaseOrder, mm: SessionPrincipal
    ) -> None:
        sent = send(mm, issued)
        services.amend(mm, issued.pk, reason="More", header=services.HeaderAmendment(notes="x"))
        assert EmailOutbox.objects.get(pk=sent.pk).status == Status.SUPERSEDED
        assert len(rows(issued)) == 1  # the new revision waits for a send by hand
        event = AuditEvent.objects.get(action="purchase_order.amended")
        assert event.data["dispatch"] == Queued.MANUAL

    def test_off_leaves_a_queued_revision_to_the_worker(
        self, issued: PurchaseOrder, mm: SessionPrincipal, settings: Any
    ) -> None:
        sent = send(mm, issued)
        settings.PO_DISPATCH = "off"
        with transaction.atomic():
            assert queue_order(PurchaseOrder.objects.get(pk=issued.pk)) == Queued.OFF
        assert EmailOutbox.objects.get(pk=sent.pk).status == Status.QUEUED


# --- the send ---------------------------------------------------------------------------------


class TestSend:
    def test_queues_the_current_revision_to_the_supplier(
        self, issued: PurchaseOrder, mm: SessionPrincipal, org: Org
    ) -> None:
        seen: list[events.Event] = []
        unsubscribe = events.subscribe("purchase_order.dispatch_queued", seen.append)
        try:
            message = send(mm, issued, note=" Asked by phone ")
        finally:
            unsubscribe()
        assert (message.kind, message.status, message.recipient_address, message.source_ref) == (
            Kind.PO_ORDER,
            Status.QUEUED,
            org.supplier.email,
            f"PO:{issued.pk}:rev0",
        )
        assert message.message_id.startswith(f"<po-{issued.pk}-rev0@")
        assert str(issued.number) in message.subject
        for text in (org.supplier.name, "CAB-25"):
            assert text in message.body and text in message.html_body
        event = AuditEvent.objects.get(action="purchase_order.dispatch_sent_by_hand")
        assert event.object_id == issued.pk
        assert event.data == {
            "number": issued.number,
            "dispatch_id": str(message.pk),
            "kind": Kind.PO_ORDER,
            "revision": 0,
            "address": org.supplier.email,
            "note": "Asked by phone",
            "repeat": False,
        }
        assert [item.payload for item in seen] == [{"po_id": issued.pk, "dispatch_id": message.pk}]

    def test_the_content_is_the_one_issue_would_queue(
        self, issued: PurchaseOrder, mm: SessionPrincipal, settings: Any
    ) -> None:
        by_hand = send(mm, issued)
        EmailOutbox.objects.filter(pk=by_hand.pk).update(status=Status.CANCELLED)
        settings.PO_DISPATCH = "on"
        with transaction.atomic():
            queue_order(PurchaseOrder.objects.get(pk=issued.pk))
        automatic = rows(issued)[-1]
        assert (by_hand.subject, by_hand.body, by_hand.html_body) == (
            automatic.subject,
            automatic.body,
            automatic.html_body,
        )

    def test_the_address_is_the_suppliers_current_one(
        self, issued: PurchaseOrder, mm: SessionPrincipal, org: Org
    ) -> None:
        org.supplier.email = NEW_ADDRESS
        org.supplier.save()
        assert send(mm, issued).recipient_address == NEW_ADDRESS
        assert issued.supplier_snapshot["email"] != NEW_ADDRESS  # the snapshot stays as issued

    def test_works_under_on_too(
        self, buy: Buy, make_line: MakeLine, mm: SessionPrincipal, settings: Any, system: Any
    ) -> None:
        settings.PO_DISPATCH = "on"
        order = buy((make_line("10"), "10"))
        record_outcome(system, claim(system).attempt_id, Outcome.SENT)
        again = send(mm, order)
        assert again.status == Status.QUEUED and len(rows(order)) == 2

    @pytest.mark.parametrize("outcome", [Outcome.SENT, Outcome.FAILED, Outcome.AMBIGUOUS])
    def test_a_repeat_after_the_revision_ended_gets_a_fresh_message_id(
        self,
        issued: PurchaseOrder,
        mm: SessionPrincipal,
        system: SystemPrincipal,
        outcome: Outcome,
    ) -> None:
        first = send(mm, issued)
        record_outcome(system, claim(system).attempt_id, outcome, error="x")
        EmailOutbox.objects.filter(pk=first.pk).update(status=outcome)  # FAILED: retries used
        second = send(mm, issued)
        assert second.message_id != first.message_id
        assert second.message_id.startswith(f"<po-{issued.pk}-rev0-")
        assert second.source_ref == first.source_ref
        event = AuditEvent.objects.filter(action="purchase_order.dispatch_sent_by_hand").latest(
            "id"
        )
        assert event.data["repeat"] is True

    def test_amend_then_send_is_the_first_dispatch_of_the_new_revision(
        self, issued: PurchaseOrder, mm: SessionPrincipal, system: SystemPrincipal
    ) -> None:
        send(mm, issued)
        record_outcome(system, claim(system).attempt_id, Outcome.SENT)
        services.amend(mm, issued.pk, reason="More", header=services.HeaderAmendment(notes="x"))
        message = send(mm, issued)
        assert message.source_ref == f"PO:{issued.pk}:rev1"
        assert message.message_id.startswith(f"<po-{issued.pk}-rev1@")
        assert "rev1-" not in message.message_id

    def test_the_history_shows_it(
        self, issued: PurchaseOrder, mm: SessionPrincipal, people: People
    ) -> None:
        send(mm, issued, note="By phone")
        entries = selectors.history(AccessContext.for_user(people.mm_mad), issued)
        with translation.override("en"):
            assert str(entries[-1].label) == "Queued to the supplier from the order page"
        assert entries[-1].detail == "#0 · By phone"


# --- refusals ---------------------------------------------------------------------------------


class TestRefusals:
    def test_off(self, issued: PurchaseOrder, mm: SessionPrincipal, settings: Any) -> None:
        settings.PO_DISPATCH = "off"
        assert refused(mm, issued) == {"dispatch_off"}

    def test_a_draft(self, manual: None, buy: Buy, make_line: MakeLine, mm: Any) -> None:
        assert refused(mm, buy((make_line("10"), "10"), issue=False)) == {"not_issued"}

    @pytest.mark.parametrize("status", [OrderStatus.CANCELLED, OrderStatus.CLOSED])
    def test_an_order_that_ended(
        self, issued: PurchaseOrder, mm: SessionPrincipal, status: str
    ) -> None:
        PurchaseOrder.objects.filter(pk=issued.pk).update(status=status)
        assert refused(mm, issued) == {"not_issued"}

    def test_a_supplier_without_an_address(
        self, issued: PurchaseOrder, mm: SessionPrincipal, org: Org
    ) -> None:
        org.supplier.email = ""
        org.supplier.save()
        assert refused(mm, issued) == {"no_address"}

    def test_while_the_revision_is_queued(
        self, issued: PurchaseOrder, mm: SessionPrincipal
    ) -> None:
        send(mm, issued)
        assert refused(mm, issued) == {"already_queued"}

    def test_while_the_revision_is_being_sent(
        self, issued: PurchaseOrder, mm: SessionPrincipal, system: SystemPrincipal
    ) -> None:
        send(mm, issued)
        claim(system)
        assert refused(mm, issued) == {"already_queued"}

    def test_a_queued_dispatch_of_another_revision_does_not_block(
        self, issued: PurchaseOrder, mm: SessionPrincipal
    ) -> None:
        make_dispatch(issued, revision=issued.revision + 1)
        assert send(mm, issued).status == Status.QUEUED


# --- idempotency and replays ------------------------------------------------------------------


class TestIdempotency:
    def test_a_double_tap_queues_one_row(self, issued: PurchaseOrder, mm: SessionPrincipal) -> None:
        key = issue_key(mm.user_id, "purchase_order.send")
        first = send(mm, issued, idempotency_key=key, note="x")
        again = send(mm, issued, idempotency_key=key, note="x")
        assert again.pk == first.pk
        assert len(rows(issued)) == 1

    def test_the_same_key_with_another_note_is_a_conflict(
        self, issued: PurchaseOrder, mm: SessionPrincipal
    ) -> None:
        key = issue_key(mm.user_id, "purchase_order.send")
        send(mm, issued, idempotency_key=key, note="x")
        with pytest.raises(IdempotencyConflict):
            send(mm, issued, idempotency_key=key, note="y")

    def test_a_replay_after_the_grant_is_revoked_is_refused(
        self, issued: PurchaseOrder, people: People
    ) -> None:
        principal = principal_for(people.mm_mad)
        key = issue_key(principal.user_id, "purchase_order.send")
        send(principal, issued, idempotency_key=key)
        Membership.objects.filter(user=people.mm_mad).update(revoked_at=timezone.now())
        with pytest.raises(NotInScope):
            send(principal_for(people.mm_mad), issued, idempotency_key=key)
        assert len(rows(issued)) == 1


# --- what happens next ------------------------------------------------------------------------


class TestAfterwards:
    def test_the_worker_delivers_it_once(
        self, issued: PurchaseOrder, mm: SessionPrincipal, system: SystemPrincipal, tmp_path: Path
    ) -> None:
        worker = Worker(
            principal=system,
            flags_dir=tmp_path,
            heartbeat_file=tmp_path / "heartbeat",
            outbound=True,
            poll_interval=0.01,
            reconcile_interval=60,
            housekeeping_interval=3600,
            name="test-worker",
        )
        message = send(mm, issued)
        assert worker.tick() is True
        assert worker.tick() is False
        assert EmailOutbox.objects.get(pk=message.pk).status == Status.SENT
        (sent,) = mail.outbox
        assert sent.to == [message.recipient_address]
        assert str(issued.number) in sent.subject
        assert sent.extra_headers["Message-ID"] == message.message_id

    def test_a_cancel_after_a_send_that_reached_the_supplier_owes_the_notice(
        self, issued: PurchaseOrder, mm: SessionPrincipal, system: SystemPrincipal, org: Org
    ) -> None:
        org.supplier.email = NEW_ADDRESS
        org.supplier.save()
        send(mm, issued)
        record_outcome(system, claim(system).attempt_id, Outcome.SENT)
        order = services.cancel(mm, issued.pk, reason="Stop")
        assert order.cancellation_notice_required is True
        (notice,) = rows(issued, Kind.PO_CANCELLATION)
        assert notice.recipient_address == NEW_ADDRESS

    def test_a_cancel_withdraws_a_send_still_queued(
        self, issued: PurchaseOrder, mm: SessionPrincipal
    ) -> None:
        message = send(mm, issued)
        order = services.cancel(mm, issued.pk, reason="Stop")
        assert EmailOutbox.objects.get(pk=message.pk).status == Status.CANCELLED
        assert (order.cancellation_notice_required, rows(issued, Kind.PO_CANCELLATION)) == (
            False,
            [],
        )
