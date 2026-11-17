"""The supplier email on the order page (M14.4, Q82/Q83/Q86).

*Send to the supplier* beside *Printable page* on an issued order under ``PO_DISPATCH``
``manual`` and ``on``; what issue, amendment, send and cancel say about the supplier's emails
(worked out from the order after the call, so a replay says the same); what the issue bar and
the amendment page say before; the dispatch status that reloads itself while in flight.
"""

from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse

from apps.access.principals import SessionPrincipal, SystemPrincipal
from apps.core.idempotency import issue_key
from apps.notifications.models import EmailOutbox, Outcome
from apps.notifications.models import Status as MailStatus
from apps.notifications.services import Claim, claim_next, record_outcome
from apps.purchasing import services, views
from apps.purchasing.forms import SendOrderForm
from apps.purchasing.models import PurchaseOrder, PurchaseOrderLine
from tests.org import Org, People
from tests.outbox import make_dispatch
from tests.unit.purchasing.conftest import Buy, MakeLine
from tests.unit.purchasing.test_views import (
    amend_data,
    client_for,
    hidden,
    messages_of,
    page,
    post,
)

pytestmark = pytest.mark.django_db

HTMX: dict[str, Any] = {"HTTP_HX_REQUEST": "true"}
OFF_ON = ["off", "manual", "on"]


@pytest.fixture
def manager(people: People) -> Client:
    return client_for(people.mm_mad)


@pytest.fixture
def mode(request: pytest.FixtureRequest, settings: Any) -> str:
    """``PO_DISPATCH`` for the test (``manual`` unless parametrized indirectly)."""
    value = getattr(request, "param", "manual")
    settings.PO_DISPATCH = value
    return str(value)


@pytest.fixture
def issued(mode: str, buy: Buy, make_line: MakeLine) -> PurchaseOrder:
    return buy((make_line("10"), "10"))


def queued(order: PurchaseOrder) -> list[EmailOutbox]:
    return list(
        EmailOutbox.objects.filter(source_po=order, status=MailStatus.QUEUED).order_by("created_at")
    )


def send_key(people: People) -> str:
    return issue_key(people.mm_mad.pk, SendOrderForm.operation)


def send(client: Client, order: PurchaseOrder, key: str, **extra: Any) -> Any:
    return client.post(
        reverse("purchasing:send_order", args=[order.pk]),
        {"idempotency_key": key, "note": ""},
        follow=not extra,
        **extra,
    )


def sent_by_worker(outcome: Outcome = Outcome.SENT) -> EmailOutbox:
    system = SystemPrincipal()
    claimed = claim_next(system)
    assert isinstance(claimed, Claim)
    record_outcome(system, claimed.attempt_id, outcome)
    return EmailOutbox.objects.get(pk=claimed.message_id)


# --- the page ---------------------------------------------------------------------------------


class TestPage:
    @pytest.mark.parametrize("mode", OFF_ON, indirect=True)
    def test_the_button_is_offered_unless_off(
        self, manager: Client, issued: PurchaseOrder, mode: str
    ) -> None:
        html = page(manager, "detail", issued.pk)
        assert ('data-testid="send-open"' in html) is (mode != "off")
        assert ('data-testid="order-dispatches"' in html) is (mode != "off")

    def test_the_dialog_says_where_and_what(
        self, manager: Client, issued: PurchaseOrder, org: Org
    ) -> None:
        html = page(manager, "detail", issued.pk)
        dialog = html.split('data-testid="send-form"', 1)[1].split("</form>", 1)[0]
        assert org.supplier.email in dialog
        assert reverse("purchasing:send_order", args=[issued.pk]) in html
        assert "Not emailed yet." in dialog and 'data-testid="send-prices"' not in dialog
        assert 'name="idempotency_key"' in dialog and 'name="note"' in dialog

    def test_the_dialog_shows_the_latest_email_and_the_prices(
        self, manager: Client, issued: PurchaseOrder, mm: SessionPrincipal
    ) -> None:
        PurchaseOrderLine.objects.filter(po=issued).update(unit_price=Decimal("2.5"))
        services.send_order(mm, issued.pk, idempotency_key=None)
        sent_by_worker()
        dialog = page(manager, "detail", issued.pk).split('data-testid="send-form"', 1)[1]
        assert "Sent ·" in " ".join(dialog.split()[:400])
        assert 'data-testid="send-prices"' in dialog

    def test_not_for_a_warehouse_operator_who_still_sees_the_panel(
        self, people: People, issued: PurchaseOrder
    ) -> None:
        html = page(client_for(people.wo_mad), "detail", issued.pk)
        assert 'data-testid="send-open"' not in html
        assert 'data-testid="order-dispatches"' in html and "Not sent by email yet." in html

    def test_not_on_a_draft_or_a_cancelled_order(
        self, manager: Client, mode: str, buy: Buy, make_line: MakeLine, mm: SessionPrincipal
    ) -> None:
        draft = buy((make_line("10"), "10"), issue=False)
        assert 'data-testid="send-open"' not in page(manager, "detail", draft.pk)
        order = buy((make_line("10"), "10"))
        services.cancel(mm, order.pk, reason="Stop")
        assert 'data-testid="send-open"' not in page(manager, "detail", order.pk)

    def test_without_an_address_the_panel_says_why_and_leads_to_the_supplier(
        self, manager: Client, people: People, issued: PurchaseOrder, org: Org
    ) -> None:
        org.supplier.email = ""
        org.supplier.save()
        html = page(manager, "detail", issued.pk)
        assert 'data-testid="send-open"' not in html
        assert 'data-testid="dispatch-no-address"' in html
        assert reverse("suppliers:detail", args=[org.supplier.pk]) in html
        operator = page(client_for(people.wo_mad), "detail", issued.pk)
        assert 'data-testid="dispatch-no-address"' not in operator  # not theirs to send

    @pytest.mark.parametrize(
        ("mode", "said"),
        [
            ("off", None),
            ("manual", "Issuing emails nothing"),
            ("on", "Issuing emails the order to"),
        ],
        indirect=["mode"],
    )
    def test_the_issue_bar_says_where_the_order_will_go(
        self, manager: Client, mode: str, said: str | None, buy: Buy, make_line: MakeLine, org: Org
    ) -> None:
        html = page(manager, "detail", buy((make_line("10"), "10"), issue=False).pk)
        if said is None:
            assert 'data-testid="dispatch-before"' not in html
            return
        assert said in html
        assert (org.supplier.email in html) is (mode == "on")

    @pytest.mark.parametrize("mode", ["on"], indirect=True)
    def test_the_issue_bar_says_when_nothing_will_be_emailed(
        self, manager: Client, mode: str, buy: Buy, make_line: MakeLine, org: Org
    ) -> None:
        org.supplier.email = ""
        org.supplier.save()
        html = page(manager, "detail", buy((make_line("10"), "10"), issue=False).pk)
        assert "The supplier has no email address: issuing emails nothing." in html

    @pytest.mark.parametrize(
        ("mode", "said"),
        [
            ("off", None),
            ("manual", "Saving emails nothing"),
            ("on", "Saving emails the new revision to"),
        ],
        indirect=["mode"],
    )
    def test_the_amendment_page_says_where_the_revision_will_go(
        self, manager: Client, issued: PurchaseOrder, mode: str, said: str | None
    ) -> None:
        html = page(manager, "amendment", issued.pk)
        assert ('data-testid="dispatch-before"' in html) is (said is not None)
        if said:
            assert said in html


# --- the send ---------------------------------------------------------------------------------


class TestSend:
    def test_queues_it_and_names_the_address(
        self, manager: Client, people: People, issued: PurchaseOrder, org: Org
    ) -> None:
        response = send(manager, issued, send_key(people))
        assert response.redirect_chain[-1][0] == reverse("purchasing:detail", args=[issued.pk])
        assert messages_of(response) == [
            f"{issued.number}: Queued to be emailed to {org.supplier.email}."
        ]
        assert len(queued(issued)) == 1

    def test_a_replay_says_the_same_and_queues_nothing_more(
        self, manager: Client, people: People, issued: PurchaseOrder
    ) -> None:
        key = send_key(people)
        first = messages_of(send(manager, issued, key))
        assert messages_of(send(manager, issued, key)) == first
        assert len(queued(issued)) == 1

    def test_a_replay_after_the_worker_sent_it_says_so(
        self, manager: Client, people: People, issued: PurchaseOrder, org: Org
    ) -> None:
        key = send_key(people)
        send(manager, issued, key)
        sent_by_worker()
        assert messages_of(send(manager, issued, key)) == [
            f"{issued.number}: The email to {org.supplier.email} is sent: see Sent to the supplier."
        ]

    def test_from_the_dialog(self, manager: Client, people: People, issued: PurchaseOrder) -> None:
        response = send(manager, issued, send_key(people), **HTMX)
        assert response.status_code == 204
        assert response["HX-Redirect"] == reverse("purchasing:detail", args=[issued.pk])

    def test_a_refusal_is_flashed_or_shown_in_the_dialog(
        self, manager: Client, people: People, issued: PurchaseOrder
    ) -> None:
        send(manager, issued, send_key(people))
        response = send(manager, issued, send_key(people))
        assert messages_of(response) == [
            "This revision is already on its way to the supplier: wait for its status."
        ]
        again = send(manager, issued, send_key(people), **HTMX)
        assert again.status_code == 422
        assert "already on its way" in again.content.decode()
        assert len(queued(issued)) == 1

    def test_an_invalid_form_comes_back_in_the_dialog(
        self, manager: Client, issued: PurchaseOrder, org: Org
    ) -> None:
        response = manager.post(
            reverse("purchasing:send_order", args=[issued.pk]),
            {"note": "x" * 501},
            **HTMX,
        )
        assert response.status_code == 422
        assert org.supplier.email in response.content.decode()
        assert queued(issued) == []

    def test_a_forged_key_is_refused(self, manager: Client, issued: PurchaseOrder) -> None:
        response = send(manager, issued, "forged", HTTP_ACCEPT="text/html")
        assert response.status_code == 400 and queued(issued) == []


# --- what each action says --------------------------------------------------------------------


class TestIssueMessages:
    def issue(self, manager: Client, draft: PurchaseOrder) -> list[str]:
        key = hidden(page(manager, "detail", draft.pk), "idempotency_key", "issue-form")
        data = {"idempotency_key": key, "confirm": "on"}
        first = messages_of(post(manager, "issue", draft.pk, data=data))
        assert messages_of(post(manager, "issue", draft.pk, data=data)) == first  # a replay
        return first

    @pytest.fixture
    def draft(self, buy: Buy, make_line: MakeLine) -> PurchaseOrder:
        return buy((make_line("10"), "10"), issue=False)

    @pytest.mark.parametrize("mode", ["off"], indirect=True)
    def test_off_keeps_todays_message(
        self, manager: Client, mode: str, draft: PurchaseOrder
    ) -> None:
        said = self.issue(manager, draft)
        number = PurchaseOrder.objects.get(pk=draft.pk).number
        assert said == [f"Order {number} issued."]

    def test_manual_says_to_send_it(self, manager: Client, mode: str, draft: PurchaseOrder) -> None:
        said = self.issue(manager, draft)
        assert said[1:] == ["Not emailed to the supplier: use Send to the supplier to email it."]

    @pytest.mark.parametrize("mode", ["on"], indirect=True)
    def test_on_names_the_address(
        self, manager: Client, mode: str, draft: PurchaseOrder, org: Org
    ) -> None:
        assert self.issue(manager, draft)[1:] == [f"Queued to be emailed to {org.supplier.email}."]

    @pytest.mark.parametrize("mode", ["on"], indirect=True)
    def test_on_warns_without_an_address(
        self, manager: Client, mode: str, draft: PurchaseOrder, org: Org
    ) -> None:
        org.supplier.email = ""
        org.supplier.save()
        said = self.issue(manager, draft)
        assert said[1].startswith("Not emailed to the supplier: it had no email address")


class TestAmendMessages:
    def amend(self, manager: Client, order: PurchaseOrder) -> list[str]:
        data = amend_data(manager, order, reason="Later", notes="Deliver in the morning")
        return messages_of(post(manager, "amend", order.pk, data=data))

    @pytest.mark.parametrize("mode", ["off"], indirect=True)
    def test_off_keeps_todays_message(
        self, manager: Client, mode: str, issued: PurchaseOrder
    ) -> None:
        assert self.amend(manager, issued) == ["Order amended: revision 1."]

    def test_manual_withdraws_an_unsent_send_and_says_to_send_again(
        self, manager: Client, mm: SessionPrincipal, issued: PurchaseOrder
    ) -> None:
        services.send_order(mm, issued.pk, idempotency_key=None)
        assert self.amend(manager, issued) == [
            "Order amended: revision 1.",
            "An email to the supplier, not sent yet, was withdrawn.",
            "Not emailed to the supplier: use Send to the supplier to email it.",
        ]

    @pytest.mark.parametrize("mode", ["on"], indirect=True)
    def test_on_queues_the_revision_and_withdraws_the_last(
        self, manager: Client, mode: str, issued: PurchaseOrder, org: Org
    ) -> None:
        assert self.amend(manager, issued) == [
            "Order amended: revision 1.",
            "An email to the supplier, not sent yet, was withdrawn.",
            f"Queued to be emailed to {org.supplier.email}.",
        ]

    @pytest.mark.parametrize("mode", ["on"], indirect=True)
    def test_on_after_the_last_was_sent_withdraws_nothing(
        self, manager: Client, mode: str, issued: PurchaseOrder, org: Org
    ) -> None:
        sent_by_worker()
        assert self.amend(manager, issued) == [
            "Order amended: revision 1.",
            f"Queued to be emailed to {org.supplier.email}.",
        ]


class TestCancelMessages:
    def cancel(self, manager: Client, order: PurchaseOrder) -> list[str]:
        return messages_of(post(manager, "cancel", order.pk, data={"reason": "Stop"}))

    @pytest.mark.parametrize("mode", ["off"], indirect=True)
    def test_off_keeps_todays_message(
        self, manager: Client, mode: str, issued: PurchaseOrder
    ) -> None:
        make_dispatch(issued)
        assert self.cancel(manager, issued) == ["Order cancelled."]

    def test_an_unsent_email_is_withdrawn(
        self, manager: Client, mm: SessionPrincipal, issued: PurchaseOrder
    ) -> None:
        services.send_order(mm, issued.pk, idempotency_key=None)
        assert self.cancel(manager, issued) == [
            "Order cancelled.",
            "An email to the supplier, not sent yet, was withdrawn.",
        ]

    def test_a_notice_is_owed_once_it_was_sent(
        self, manager: Client, mm: SessionPrincipal, issued: PurchaseOrder, org: Org
    ) -> None:
        services.send_order(mm, issued.pk, idempotency_key=None)
        sent_by_worker()
        assert self.cancel(manager, issued) == [
            "Order cancelled.",
            f"A cancellation notice is queued to be emailed to {org.supplier.email}.",
        ]

    def test_an_email_in_flight_owes_a_notice_if_it_arrives(
        self, manager: Client, mm: SessionPrincipal, issued: PurchaseOrder
    ) -> None:
        services.send_order(mm, issued.pk, idempotency_key=None)
        assert isinstance(claim_next(SystemPrincipal()), Claim)
        assert self.cancel(manager, issued)[1].startswith(
            "The order is being emailed to the supplier right now"
        )

    def test_a_draft_says_nothing_of_emails(
        self, manager: Client, mode: str, buy: Buy, make_line: MakeLine
    ) -> None:
        draft = buy((make_line("10"), "10"), issue=False)
        assert self.cancel(manager, draft) == ["Draft discarded."]


# --- the status on its way --------------------------------------------------------------------


class TestDelivery:
    def fragment(self, client: Client, message: EmailOutbox) -> Any:
        return client.get(reverse("purchasing:dispatch_delivery", args=[message.pk]))

    def test_polls_while_queued_and_stops_once_sent(
        self, manager: Client, mm: SessionPrincipal, issued: PurchaseOrder
    ) -> None:
        message = services.send_order(mm, issued.pk, idempotency_key=None)
        html = self.fragment(manager, message).content.decode()
        assert 'hx-trigger="every 5s"' in html and "data-poll" in html
        assert 'data-state="queued"' in html and message.recipient_address in html
        sent_by_worker()
        html = self.fragment(manager, message).content.decode()
        assert "hx-get" not in html and 'data-state="sent"' in html
        assert message.body not in html

    def test_slows_down_once_delayed(
        self, manager: Client, mm: SessionPrincipal, issued: PurchaseOrder
    ) -> None:
        message = services.send_order(mm, issued.pk, idempotency_key=None)
        EmailOutbox.objects.filter(pk=message.pk).update(
            not_before=message.created_at - timedelta(minutes=11)
        )
        html = self.fragment(manager, message).content.decode()
        assert 'hx-trigger="every 60s"' in html and 'data-state="delayed"' in html
        assert 'data-testid="dispatch-delayed"' in html

    def test_the_page_polls_the_same_fragment(
        self, manager: Client, mm: SessionPrincipal, issued: PurchaseOrder
    ) -> None:
        message = services.send_order(mm, issued.pk, idempotency_key=None)
        html = page(manager, "detail", issued.pk)
        assert reverse("purchasing:dispatch_delivery", args=[message.pk]) in html

    def test_a_warehouse_operator_reads_it_and_another_branch_does_not(
        self, people: People, mm: SessionPrincipal, issued: PurchaseOrder
    ) -> None:
        message = services.send_order(mm, issued.pk, idempotency_key=None)
        assert self.fragment(client_for(people.wo_mad), message).status_code == 200
        assert self.fragment(client_for(people.mm_sev), message).status_code == 404

    def test_is_passive(self) -> None:
        assert getattr(views.dispatch_delivery, "session_passive", False)


# --- what R3 asked for (M14.7) ----------------------------------------------------------------


class TestReviewFollowUps:
    def fragment(self, client: Client, message: EmailOutbox) -> str:
        url = reverse("purchasing:dispatch_delivery", args=[message.pk])
        return str(client.get(url).content.decode())

    def test_the_dialog_says_when_the_revision_is_already_on_its_way(
        self, manager: Client, mm: SessionPrincipal, issued: PurchaseOrder
    ) -> None:
        dialog = page(manager, "detail", issued.pk).split('data-testid="send-form"', 1)[1]
        assert 'data-testid="send-in-flight"' not in dialog
        services.send_order(mm, issued.pk, idempotency_key=None)
        dialog = page(manager, "detail", issued.pk).split('data-testid="send-form"', 1)[1]
        assert 'data-testid="send-in-flight"' in dialog
        sent_by_worker()
        dialog = page(manager, "detail", issued.pk).split('data-testid="send-form"', 1)[1]
        assert 'data-testid="send-in-flight"' not in dialog

    def test_a_poll_that_ends_in_a_decision_leads_its_manager_back_to_the_page(
        self, manager: Client, people: People, mm: SessionPrincipal, issued: PurchaseOrder
    ) -> None:
        message = services.send_order(mm, issued.pk, idempotency_key=None)
        assert 'data-testid="dispatch-reload"' not in self.fragment(manager, message)
        sent_by_worker(Outcome.AMBIGUOUS)
        html = self.fragment(manager, message)
        assert 'data-testid="dispatch-reload"' in html and "hx-get" not in html
        assert reverse("purchasing:detail", args=[issued.pk]) in html
        # The warehouse operator reads the status; the decision is not theirs.
        reader = self.fragment(client_for(people.wo_mad), message)
        assert 'data-state="ambiguous"' in reader and 'data-testid="dispatch-reload"' not in reader

    def test_a_retry_after_a_failed_attempt_counts_as_delayed(
        self, manager: Client, mm: SessionPrincipal, issued: PurchaseOrder
    ) -> None:
        message = services.send_order(mm, issued.pk, idempotency_key=None)
        EmailOutbox.objects.filter(pk=message.pk).update(
            attempts=1, not_before=message.created_at + timedelta(minutes=30)
        )
        html = self.fragment(manager, message)
        assert 'data-state="delayed"' in html and 'hx-trigger="every 60s"' in html
        # Sent again by a human: due at once, not delayed although it had an attempt.
        EmailOutbox.objects.filter(pk=message.pk).update(not_before=message.created_at)
        assert 'data-state="queued"' in self.fragment(manager, message)

    def test_a_cancel_counts_only_what_it_withdrew(
        self, manager: Client, mm: SessionPrincipal, issued: PurchaseOrder
    ) -> None:
        """A dispatch a human discarded after a restore is CANCELLED too: not by the cancel."""
        make_dispatch(issued, MailStatus.CANCELLED)  # discarded earlier
        said = messages_of(post(manager, "cancel", issued.pk, data={"reason": "Stop"}))
        assert said == ["Order cancelled."]

    def test_no_send_again_once_another_email_of_the_revision_is_on_its_way(
        self, manager: Client, mm: SessionPrincipal, issued: PurchaseOrder
    ) -> None:
        failed = make_dispatch(issued, MailStatus.FAILED)
        assert 'data-testid="dispatch-resend-open"' in page(manager, "detail", issued.pk)
        services.send_order(mm, issued.pk, idempotency_key=None)
        html = page(manager, "detail", issued.pk)
        assert 'data-testid="dispatch-resend-open"' not in html
        assert str(failed.pk) in html  # still listed, with its status
