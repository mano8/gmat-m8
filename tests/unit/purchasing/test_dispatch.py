"""Supplier dispatches of purchase orders (plan §5, §4.7 L4b; M5.3, Q7 behind ``PO_DISPATCH``).

Queued at issue and at each amendment from the snapshots, superseded by a newer revision,
cancelled with the order or answered with a cancellation notice; claimed and settled by the
worker through the purchasing app's source, every attempt audited on the order; confirmed or
resent by a human once the worker left one ``AMBIGUOUS`` or ``FAILED``. The races and a real
crash are in ``tests/integration/test_po_dispatch.py``; the access matrix of the decision
services is ``test_matrix``.
"""

from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from django.core import mail
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone, translation

from apps.access.context import AccessContext
from apps.access.exceptions import NotInScope
from apps.access.principals import SessionPrincipal, SystemPrincipal
from apps.audit.models import AuditEvent
from apps.catalog.models import Material
from apps.notifications.models import EmailAttempt, EmailOutbox, Kind, Outcome, Status
from apps.notifications.services import (
    Claim,
    Settled,
    claim_next,
    reconcile_abandoned,
    record_outcome,
)
from apps.notifications.worker import Worker
from apps.purchasing import dispatch, selectors, services
from apps.purchasing.models import PurchaseOrder
from apps.purchasing.services import LineAmendment
from tests.org import Org, People
from tests.outbox import make_dispatch
from tests.support import error_codes
from tests.unit.purchasing.conftest import Buy, FakeReceipts, MakeLine, allocation, only_line

pytestmark = pytest.mark.django_db

D = Decimal
OrderStatus = PurchaseOrder.Status


@pytest.fixture
def dispatch_on(settings: Any) -> None:
    settings.PO_DISPATCH = True


@pytest.fixture
def system() -> SystemPrincipal:
    return SystemPrincipal()


@pytest.fixture
def worker(tmp_path: Path, system: SystemPrincipal) -> Worker:
    """Sends through the test settings' locmem mailer (``mail.outbox``)."""
    return Worker(
        principal=system,
        flags_dir=tmp_path,
        heartbeat_file=tmp_path / "heartbeat",
        outbound=True,
        poll_interval=0.01,
        reconcile_interval=60,
        housekeeping_interval=3600,
        name="test-worker",
    )


@pytest.fixture
def issued(dispatch_on: None, buy: Buy, make_line: MakeLine) -> PurchaseOrder:
    """An issued order of 10 m of cable, queued to the supplier."""
    return buy((make_line("10"), "10"))


def dispatches(order: PurchaseOrder, kind: Kind = Kind.PO_ORDER) -> list[EmailOutbox]:
    return list(EmailOutbox.objects.filter(source_po=order, kind=kind).order_by("created_at", "id"))


def notices(order: PurchaseOrder) -> list[EmailOutbox]:
    return dispatches(order, Kind.PO_CANCELLATION)


def reload(message: EmailOutbox) -> EmailOutbox:
    return EmailOutbox.objects.get(pk=message.pk)


def claim(system: SystemPrincipal) -> Claim:
    claimed = claim_next(system)
    assert isinstance(claimed, Claim), claimed
    return claimed


def ended(system: SystemPrincipal, outcome: Outcome) -> EmailOutbox:
    """Claim the next dispatch and record ``outcome`` for it."""
    claimed = claim(system)
    error = "" if outcome == Outcome.SENT else "timeout after DATA"
    record_outcome(system, claimed.attempt_id, outcome, error=error)
    return EmailOutbox.objects.get(pk=claimed.message_id)


def actions(order: PurchaseOrder) -> list[str]:
    return list(
        AuditEvent.objects.filter(object_id=order.pk)
        .order_by("id")
        .values_list("action", flat=True)
    )


def ctx(user: object) -> AccessContext:
    return AccessContext.for_user(user)  # type: ignore[arg-type]


def codes(call: Callable[..., Any], *args: Any, **kwargs: Any) -> set[str]:
    with pytest.raises(ValidationError) as exc:
        call(*args, **kwargs)
    return error_codes(exc)


# --- queueing at issue and amendment ----------------------------------------------------


class TestIssue:
    def test_queues_the_order_from_its_snapshots(
        self, issued: PurchaseOrder, org: Org, cable: Material
    ) -> None:
        (message,) = dispatches(issued)
        assert (message.status, message.recipient_address, message.source_ref) == (
            Status.QUEUED,
            org.supplier.email,
            f"PO:{issued.pk}:rev0",
        )
        assert message.message_id.startswith(f"<po-{issued.pk}-rev0@")
        assert str(issued.number) in message.subject
        for text in (org.supplier.name, issued.buyer_snapshot["company"]["legal_name"], "CAB-25"):
            assert text in message.body
            assert text in message.html_body
        event = AuditEvent.objects.get(action="purchase_order.issued")
        assert event.data["dispatch"] == dispatch.Queued.QUEUED

    def test_nothing_is_queued_while_the_switch_is_off(self, buy: Buy, make_line: MakeLine) -> None:
        order = buy((make_line("10"), "10"))
        assert dispatches(order) == []
        event = AuditEvent.objects.get(action="purchase_order.issued")
        assert event.data["dispatch"] == dispatch.Queued.OFF

    def test_a_supplier_without_an_address_is_not_queued(
        self, dispatch_on: None, buy: Buy, make_line: MakeLine, org: Org
    ) -> None:
        org.supplier.email = ""
        org.supplier.save()
        order = buy((make_line("10"), "10"))
        assert dispatches(order) == []
        event = AuditEvent.objects.get(action="purchase_order.issued")
        assert event.data["dispatch"] == dispatch.Queued.NO_ADDRESS

    def test_prices_go_to_the_supplier_only_when_recorded(
        self, dispatch_on: None, mm: SessionPrincipal, buy: Buy, make_line: MakeLine
    ) -> None:
        plain = buy((make_line("10"), "10"))
        priced = buy((make_line("10"), "10"), issue=False)
        services.update_line(mm, only_line(priced).pk, unit_price=D("2.5"))
        services.issue(mm, priced.pk)
        assert "EUR" not in dispatches(plain)[0].body
        (message,) = dispatches(priced)
        assert "EUR" in message.body
        assert "EUR" in message.html_body

    def test_the_text_body_is_not_escaped_and_the_html_body_is(
        self, dispatch_on: None, buy: Buy, make_line: MakeLine, org: Org
    ) -> None:
        org.supplier.name = "Cables & <Co>"
        org.supplier.save()
        (message,) = dispatches(buy((make_line("10"), "10")))
        assert "Cables & <Co>" in message.body
        assert "Cables &amp; &lt;Co&gt;" in message.html_body
        assert "<Co>" not in message.html_body

    def test_the_content_is_in_the_installation_language(self, issued: PurchaseOrder) -> None:
        (message,) = dispatches(issued)
        assert message.subject.startswith("Pedido de compra")


class TestAmend:
    def test_supersedes_the_queued_revision_without_changing_its_content(
        self, issued: PurchaseOrder, mm: SessionPrincipal
    ) -> None:
        (first,) = dispatches(issued)
        services.amend(
            mm,
            issued.pk,
            reason="Morning",
            header=services.HeaderAmendment(notes="Deliver in the morning"),
        )
        old, new = dispatches(issued)
        assert (old.pk, old.status) == (first.pk, Status.SUPERSEDED)
        assert (old.subject, old.body, old.html_body, old.message_id) == (
            first.subject,
            first.body,
            first.html_body,
            first.message_id,
        )
        assert (new.status, new.source_ref) == (Status.QUEUED, f"PO:{issued.pk}:rev1")
        assert new.message_id.startswith(f"<po-{issued.pk}-rev1@")
        assert "Deliver in the morning" in new.body
        assert "1" in new.subject
        event = AuditEvent.objects.get(action="purchase_order.amended")
        assert event.data["dispatch"] == dispatch.Queued.QUEUED

    @pytest.mark.parametrize("status", [Status.SENDING, Status.SENT, Status.AMBIGUOUS])
    def test_never_changes_a_dispatch_that_may_have_left(
        self, issued: PurchaseOrder, mm: SessionPrincipal, system: SystemPrincipal, status: Status
    ) -> None:
        (first,) = dispatches(issued)
        claimed = claim(system)
        if status != Status.SENDING:
            outcome = Outcome.SENT if status == Status.SENT else Outcome.AMBIGUOUS
            record_outcome(system, claimed.attempt_id, outcome)
        services.amend(mm, issued.pk, reason="More", header=services.HeaderAmendment(notes="x"))
        assert reload(first).status == status
        assert [message.status for message in dispatches(issued)] == [status, Status.QUEUED]

    def test_the_address_is_the_one_the_order_was_issued_to(
        self, issued: PurchaseOrder, mm: SessionPrincipal, org: Org
    ) -> None:
        original = org.supplier.email
        org.supplier.email = "new-address@supplier.example"
        org.supplier.save()
        services.amend(mm, issued.pk, reason="Later", header=services.HeaderAmendment(notes="x"))
        assert {message.recipient_address for message in dispatches(issued)} == {original}

    def test_a_no_op_amendment_queues_nothing(
        self, issued: PurchaseOrder, mm: SessionPrincipal
    ) -> None:
        services.amend(mm, issued.pk, reason="Nothing")
        assert len(dispatches(issued)) == 1

    def test_supersedes_even_with_the_switch_off_since(
        self, issued: PurchaseOrder, mm: SessionPrincipal, settings: Any
    ) -> None:
        """Turning the switch off stops new dispatches; queued ones still go out."""
        settings.PO_DISPATCH = False
        services.amend(mm, issued.pk, reason="Later", header=services.HeaderAmendment(notes="x"))
        assert [message.status for message in dispatches(issued)] == [Status.QUEUED]
        event = AuditEvent.objects.get(action="purchase_order.amended")
        assert event.data["dispatch"] == dispatch.Queued.OFF


# --- cancellation -----------------------------------------------------------------------


class TestCancel:
    def test_while_queued_nothing_is_sent(
        self, issued: PurchaseOrder, mm: SessionPrincipal, worker: Worker
    ) -> None:
        (message,) = dispatches(issued)
        order = services.cancel(mm, issued.pk, reason="Not needed")
        assert reload(message).status == Status.CANCELLED
        assert (order.cancellation_notice_required, notices(issued)) == (False, [])
        assert worker.tick() is False
        assert mail.outbox == []

    @pytest.mark.parametrize("outcome", [Outcome.SENT, Outcome.AMBIGUOUS])
    def test_after_the_supplier_may_have_it_the_notice_is_queued_at_once(
        self,
        issued: PurchaseOrder,
        mm: SessionPrincipal,
        system: SystemPrincipal,
        settings: Any,
        outcome: Outcome,
    ) -> None:
        sent = ended(system, outcome)
        settings.PO_DISPATCH = False  # the notice is owed whatever the switch says
        order = services.cancel(mm, issued.pk, reason="Not needed")
        assert order.cancellation_notice_required is True
        (notice,) = notices(issued)
        assert (notice.status, notice.recipient_address, notice.source_ref) == (
            Status.QUEUED,
            sent.recipient_address,
            f"PO:{issued.pk}:cancel",
        )
        assert notice.message_id.startswith(f"<po-{issued.pk}-cancel@")
        assert str(issued.number) in notice.subject
        assert str(issued.number) in notice.body
        event = AuditEvent.objects.get(action="purchase_order.cancelled")
        assert event.data["cancellation_notice_queued"] is True

    def test_while_sending_the_notice_follows_the_outcome_once(
        self, issued: PurchaseOrder, mm: SessionPrincipal, system: SystemPrincipal, worker: Worker
    ) -> None:
        first = claim(system)
        services.amend(mm, issued.pk, reason="More", header=services.HeaderAmendment(notes="x"))
        second = claim(system)  # both revisions are being sent
        order = services.cancel(mm, issued.pk, reason="Not needed")
        assert (order.cancellation_notice_required, notices(issued)) == (True, [])
        record_outcome(system, first.attempt_id, Outcome.SENT)
        record_outcome(system, second.attempt_id, Outcome.AMBIGUOUS)
        (notice,) = notices(issued)
        assert worker.tick() is True
        assert worker.tick() is False
        assert reload(notice).status == Status.SENT
        assert [message.subject for message in mail.outbox] == [notice.subject]

    def test_while_sending_a_failure_owes_nothing_and_is_not_retried(
        self, issued: PurchaseOrder, mm: SessionPrincipal, system: SystemPrincipal
    ) -> None:
        claimed = claim(system)
        services.cancel(mm, issued.pk, reason="Not needed")
        record_outcome(system, claimed.attempt_id, Outcome.FAILED, error="refused")
        assert notices(issued) == []
        message = EmailOutbox.objects.get(pk=claimed.message_id)
        assert message.status == Status.QUEUED  # the retry policy...
        EmailOutbox.objects.filter(pk=message.pk).update(not_before=timezone.now() - timedelta(1))
        assert claim_next(system) == Settled(message.pk, Status.CANCELLED)  # ...meets the order

    def test_a_dispatch_queued_again_after_an_ambiguous_attempt_owes_the_notice(
        self, issued: PurchaseOrder, mm: SessionPrincipal, system: SystemPrincipal
    ) -> None:
        """The supplier may have the first attempt: cancelling before the resend goes out
        still owes them the notice (F53)."""
        message = ended(system, Outcome.AMBIGUOUS)
        services.resend_dispatch(mm, message.pk)
        order = services.cancel(mm, issued.pk, reason="Not needed")
        assert reload(message).status == Status.CANCELLED
        assert order.cancellation_notice_required is True
        (notice,) = notices(issued)
        assert notice.recipient_address == message.recipient_address

    def test_a_dispatch_retried_after_a_failure_owes_nothing(
        self, issued: PurchaseOrder, mm: SessionPrincipal, system: SystemPrincipal
    ) -> None:
        message = ended(system, Outcome.FAILED)
        assert reload(message).status == Status.QUEUED  # the automatic retry
        order = services.cancel(mm, issued.pk, reason="Not needed")
        assert (order.cancellation_notice_required, notices(issued)) == (False, [])

    def test_an_abandoned_attempt_owes_the_notice_too(
        self, issued: PurchaseOrder, mm: SessionPrincipal, system: SystemPrincipal
    ) -> None:
        claimed = claim(system)
        services.cancel(mm, issued.pk, reason="Not needed")
        EmailAttempt.objects.filter(pk=claimed.attempt_id).update(
            started_at=timezone.now() - timedelta(hours=1)
        )
        assert reconcile_abandoned(system).abandoned == 1
        assert reload(dispatches(issued)[0]).status == Status.AMBIGUOUS
        assert len(notices(issued)) == 1


class TestClosedShort:
    def test_the_supplier_sees_what_a_closed_line_ends_up_ordering(
        self,
        dispatch_on: None,
        mm: SessionPrincipal,
        buy: Buy,
        received: FakeReceipts,
        breaker: Material,
        make_line: MakeLine,
    ) -> None:
        cable_line = make_line("10")
        order = buy((cable_line, "10"), (make_line("3", material=breaker), "3"))
        line = order.lines.get(line_no=1)
        received.receive(line, "4", {allocation(order, cable_line): "4"})
        services.close_line_short(mm, line.pk, reason="Supplier out of stock")
        services.amend(mm, order.pk, reason="Later", header=services.HeaderAmendment(notes="x"))
        message = dispatches(order)[-1]
        assert "CAB-25 Cable 2,5: 4 M (Cerrada con faltante)" in message.body
        assert "10 M" not in message.body
        assert "DIF-40 Diferencial 40A: 3 UD\n" in message.body


class TestNotice:
    def test_nothing_is_owed_while_nothing_may_have_left(self, issued: PurchaseOrder) -> None:
        with transaction.atomic():
            assert dispatch.queue_cancellation_notice(issued) is None
        assert notices(issued) == []

    def test_any_other_integrity_error_is_raised(
        self, issued: PurchaseOrder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        EmailOutbox.objects.filter(source_po=issued).update(status=Status.SENT)

        def broken(**values: Any) -> EmailOutbox:
            raise IntegrityError("notifications_outbox_shape_per_kind")

        monkeypatch.setattr(dispatch, "queue_dispatch", broken)
        with pytest.raises(IntegrityError), transaction.atomic():
            dispatch.queue_cancellation_notice(issued)


# --- the worker's source ----------------------------------------------------------------


class TestSource:
    def test_claims_the_current_revision_with_its_stable_message_id(
        self, issued: PurchaseOrder, system: SystemPrincipal
    ) -> None:
        (message,) = dispatches(issued)
        claimed = claim(system)
        assert (claimed.message_id, claimed.header_message_id, claimed.to) == (
            message.pk,
            message.message_id,
            (message.recipient_address,),
        )

    def test_an_older_revision_is_superseded_at_the_claim(
        self, issued: PurchaseOrder, system: SystemPrincipal
    ) -> None:
        (message,) = dispatches(issued)
        PurchaseOrder.objects.filter(pk=issued.pk).update(revision=1)
        assert claim_next(system) == Settled(message.pk, Status.SUPERSEDED)

    def test_a_cancelled_order_is_not_sent(
        self, issued: PurchaseOrder, system: SystemPrincipal
    ) -> None:
        (message,) = dispatches(issued)
        PurchaseOrder.objects.filter(pk=issued.pk).update(status=OrderStatus.CANCELLED)
        assert claim_next(system) == Settled(message.pk, Status.CANCELLED)

    def test_an_amendment_that_closes_the_order_still_goes_out(
        self,
        issued: PurchaseOrder,
        mm: SessionPrincipal,
        system: SystemPrincipal,
        received: FakeReceipts,
        make_line: MakeLine,
    ) -> None:
        """6 of 10 received; the buyer lowers the line to 6: the order closes, and the
        supplier must still learn that the other 4 are no longer wanted."""
        line = only_line(issued)
        request_line = issued.lines.get().allocations.get().request_line
        received.receive(line, "6", {allocation(issued, request_line): "6"})
        ended(system, Outcome.SENT)
        services.amend(
            mm, issued.pk, reason="Rest not needed", lines=[LineAmendment(line.pk, D(6))]
        )
        assert PurchaseOrder.objects.get(pk=issued.pk).status == OrderStatus.CLOSED
        claimed = claim(system)
        message = EmailOutbox.objects.get(pk=claimed.message_id)
        assert message.source_ref == f"PO:{issued.pk}:rev1"
        assert "6 M" in message.body

    def test_a_notice_goes_only_for_a_cancelled_order(
        self, issued: PurchaseOrder, buy: Buy, make_line: MakeLine, system: SystemPrincipal
    ) -> None:
        other = buy((make_line("10"), "10"))
        EmailOutbox.objects.filter(kind=Kind.PO_ORDER).update(status=Status.SENT)
        notice = make_dispatch(issued, kind=Kind.PO_CANCELLATION)
        assert claim_next(system) == Settled(notice.pk, Status.CANCELLED)
        PurchaseOrder.objects.filter(pk=other.pk).update(status=OrderStatus.CANCELLED)
        owed = make_dispatch(other, kind=Kind.PO_CANCELLATION)
        assert claim(system).message_id == owed.pk

    @pytest.mark.parametrize(
        ("outcome", "action"),
        [
            (Outcome.SENT, "purchase_order.dispatch_sent"),
            (Outcome.FAILED, "purchase_order.dispatch_failed"),
            (Outcome.AMBIGUOUS, "purchase_order.dispatch_ambiguous"),
        ],
    )
    def test_every_attempt_is_audited_on_the_order(
        self, issued: PurchaseOrder, system: SystemPrincipal, outcome: Outcome, action: str
    ) -> None:
        ended(system, outcome)
        assert actions(issued)[-2:] == ["purchase_order.dispatch_started", action]
        started, finished = AuditEvent.objects.filter(object_id=issued.pk).order_by("-id")[:2][::-1]
        assert (started.actor_label, finished.actor_label) == ("system:worker", "system:worker")
        assert (started.branch_id, started.data["attempt"]) == (issued.branch_id, 1)
        assert finished.data | {"dispatch_id": None} == {
            "number": issued.number,
            "dispatch_id": None,
            "kind": Kind.PO_ORDER,
            "revision": 0,
            "attempt": 1,
            "abandoned": False,
            "error": "" if outcome == Outcome.SENT else "timeout after DATA",
        }
        assert issued.supplier.email not in str(finished.data)

    def test_an_abandoned_attempt_is_audited(
        self, issued: PurchaseOrder, system: SystemPrincipal
    ) -> None:
        claimed = claim(system)
        EmailAttempt.objects.filter(pk=claimed.attempt_id).update(
            started_at=timezone.now() - timedelta(hours=1)
        )
        reconcile_abandoned(system)
        event = AuditEvent.objects.filter(object_id=issued.pk).latest("id")
        assert (event.action, event.data["abandoned"]) == (
            "purchase_order.dispatch_ambiguous",
            True,
        )

    def test_ambiguous_is_never_resent_automatically(
        self, issued: PurchaseOrder, system: SystemPrincipal
    ) -> None:
        message = ended(system, Outcome.AMBIGUOUS)
        assert message.status == Status.AMBIGUOUS
        assert claim_next(system) is None

    def test_the_history_shows_the_attempts(
        self, issued: PurchaseOrder, system: SystemPrincipal, people: People
    ) -> None:
        ended(system, Outcome.AMBIGUOUS)
        entries = selectors.history(ctx(people.mm_mad), issued)
        with translation.override("en"):
            assert [str(entry.label) for entry in entries][-2:] == [
                "Sending to the supplier",
                "Delivery to the supplier uncertain",
            ]
        assert entries[-1].detail == "#0 · timeout after DATA"


# --- a human's decision -----------------------------------------------------------------


class TestDecision:
    def test_confirm_an_ambiguous_dispatch(
        self, issued: PurchaseOrder, mm: SessionPrincipal, system: SystemPrincipal
    ) -> None:
        message = ended(system, Outcome.AMBIGUOUS)
        confirmed = services.confirm_dispatch(mm, message.pk, note=" Supplier has it ")
        assert reload(confirmed).status == Status.SENT
        assert reload(confirmed).sent_at is not None
        event = AuditEvent.objects.get(action="purchase_order.dispatch_confirmed")
        assert (event.object_id, event.data["note"], event.data["dispatch_id"]) == (
            issued.pk,
            "Supplier has it",
            str(message.pk),
        )

    def test_a_quarantined_dispatch_is_confirmed_resent_or_discarded(
        self, issued: PurchaseOrder, mm: SessionPrincipal
    ) -> None:
        row = make_dispatch(issued, Status.QUARANTINED)
        assert reload(services.confirm_dispatch(mm, row.pk)).status == Status.SENT
        row = make_dispatch(issued, Status.QUARANTINED)
        assert reload(services.resend_dispatch(mm, row.pk)).status == Status.QUEUED
        row = make_dispatch(issued, Status.QUARANTINED)
        assert reload(services.discard_dispatch(mm, row.pk, note=" No ")).status == Status.CANCELLED
        event = AuditEvent.objects.get(action="purchase_order.dispatch_discarded")
        assert (event.object_id, event.data["note"]) == (issued.pk, "No")

    @pytest.mark.parametrize("status", [Status.SENT, Status.QUEUED, Status.AMBIGUOUS])
    def test_only_a_quarantined_dispatch_is_discarded(
        self, issued: PurchaseOrder, mm: SessionPrincipal, status: Status
    ) -> None:
        row = make_dispatch(issued, status)
        assert codes(services.discard_dispatch, mm, row.pk) == {"not_held"}
        assert reload(row).status == status

    def test_a_quarantined_dispatch_of_a_later_revision_can_only_be_confirmed(
        self, issued: PurchaseOrder, people: People
    ) -> None:
        make_dispatch(issued, Status.QUARANTINED, revision=issued.revision + 1)
        rows = selectors.dispatches(ctx(people.mm_mad), issued)
        row = next(item for item in rows if item.dispatch.status == Status.QUARANTINED)
        assert (row.decidable, row.resendable, row.confirmable) == (True, False, True)

    def test_a_cancellation_owes_the_notice_for_a_quarantined_dispatch(
        self, issued: PurchaseOrder, mm: SessionPrincipal
    ) -> None:
        make_dispatch(issued, Status.QUARANTINED)
        cancelled = dispatch.cancel_dispatches(issued)
        assert cancelled.notice_queued is True

    @pytest.mark.parametrize("outcome", [Outcome.SENT, Outcome.FAILED])
    def test_only_an_ambiguous_dispatch_is_confirmed(
        self,
        issued: PurchaseOrder,
        mm: SessionPrincipal,
        system: SystemPrincipal,
        settings: Any,
        outcome: Outcome,
    ) -> None:
        message = ended(system, outcome)
        EmailOutbox.objects.filter(pk=message.pk).update(status=outcome)  # FAILED: no retry
        assert codes(services.confirm_dispatch, mm, message.pk) == {"not_ambiguous"}
        assert reload(message).status == outcome

    @pytest.mark.parametrize("outcome", [Outcome.AMBIGUOUS, Outcome.FAILED])
    def test_resend_makes_a_new_attempt(
        self,
        issued: PurchaseOrder,
        mm: SessionPrincipal,
        system: SystemPrincipal,
        outcome: Outcome,
    ) -> None:
        message = ended(system, outcome)
        EmailOutbox.objects.filter(pk=message.pk).update(status=outcome)
        services.resend_dispatch(mm, message.pk, note="Asked by phone")
        assert reload(message).status == Status.QUEUED
        again = claim(system)
        assert (again.message_id, again.attempt_number, again.header_message_id) == (
            message.pk,
            2,
            message.message_id,
        )
        assert "purchase_order.dispatch_resent" in actions(issued)

    def test_a_resent_dispatch_that_is_ambiguous_again_waits_again(
        self, issued: PurchaseOrder, mm: SessionPrincipal, system: SystemPrincipal
    ) -> None:
        message = ended(system, Outcome.AMBIGUOUS)
        services.resend_dispatch(mm, message.pk)
        assert ended(system, Outcome.AMBIGUOUS).status == Status.AMBIGUOUS
        assert claim_next(system) is None

    def test_only_a_failed_or_ambiguous_dispatch_is_resent(
        self, issued: PurchaseOrder, mm: SessionPrincipal, system: SystemPrincipal
    ) -> None:
        (queued,) = dispatches(issued)
        assert codes(services.resend_dispatch, mm, queued.pk) == {"not_decidable"}
        sent = ended(system, Outcome.SENT)
        assert codes(services.resend_dispatch, mm, sent.pk) == {"not_decidable"}

    def test_an_outdated_revision_is_not_resent(
        self, issued: PurchaseOrder, mm: SessionPrincipal, system: SystemPrincipal
    ) -> None:
        message = ended(system, Outcome.AMBIGUOUS)
        services.amend(mm, issued.pk, reason="More", header=services.HeaderAmendment(notes="x"))
        assert codes(services.resend_dispatch, mm, message.pk) == {"not_eligible"}
        services.cancel(mm, issued.pk, reason="Stop")
        assert codes(services.resend_dispatch, mm, message.pk) == {"not_eligible"}

    def test_the_cancellation_notice_is_resent_while_the_order_stays_cancelled(
        self, issued: PurchaseOrder, mm: SessionPrincipal, system: SystemPrincipal
    ) -> None:
        ended(system, Outcome.SENT)
        services.cancel(mm, issued.pk, reason="Stop")
        notice = ended(system, Outcome.AMBIGUOUS)
        assert notice.kind == Kind.PO_CANCELLATION
        services.resend_dispatch(mm, notice.pk)
        assert claim(system).message_id == notice.pk


class TestRows:
    def test_what_a_human_may_do_with_each_dispatch(
        self, issued: PurchaseOrder, mm: SessionPrincipal, system: SystemPrincipal, people: People
    ) -> None:
        old = ended(system, Outcome.AMBIGUOUS)
        services.amend(mm, issued.pk, reason="More", header=services.HeaderAmendment(notes="x"))
        operator = ctx(people.wo_mad)
        new_row, old_row = selectors.dispatches(operator, PurchaseOrder.objects.get(pk=issued.pk))
        assert (old_row.dispatch.pk, old_row.revision, old_row.notice) == (old.pk, 0, False)
        assert (old_row.decidable, old_row.resendable, old_row.confirmable) == (True, False, True)
        assert (new_row.revision, new_row.decidable, new_row.actionable) == (1, False, False)
        assert selectors.awaiting_decision([new_row, old_row]) is True
        assert selectors.dispatch(operator, old.pk) == old

    def test_outside_the_scope_nothing_is_found(
        self, issued: PurchaseOrder, people: People
    ) -> None:
        (message,) = dispatches(issued)
        foreign = ctx(people.mm_sev)
        assert selectors.dispatches(foreign, issued) == []
        with pytest.raises(NotInScope):
            selectors.dispatch(foreign, message.pk)
