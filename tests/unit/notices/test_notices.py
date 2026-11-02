"""Business notifications (M7.1): recipients of each event, privacy, revocation, rollback."""

from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest
from django.db import transaction
from django.utils import timezone

from apps.access.roles import Capability, Role
from apps.accounts.models import User
from apps.material_requests import services as request_services
from apps.material_requests.models import MaterialRequest, MaterialRequestLine
from apps.notifications import eligibility
from apps.notifications.models import EmailOutbox, Kind, NotificationPreference
from apps.organizations.models import Membership
from tests.org import Org, People
from tests.support import principal_for
from tests.unit.handovers.conftest import HandOver
from tests.unit.material_requests.conftest import MakeRequest
from tests.unit.purchasing.conftest import Buy, MakeLine
from tests.unit.receiving.conftest import Receive

pytestmark = pytest.mark.django_db

D = Decimal


def notices() -> list[EmailOutbox]:
    return list(EmailOutbox.objects.filter(kind=Kind.NOTIFICATION).order_by("created_at"))


def recipients_of(message: EmailOutbox) -> set[Any]:
    return set(message.recipient_users)


@pytest.fixture
def submitted(people: People, make_request: MakeRequest) -> MaterialRequest:
    """A draft of the A-MAD technician for the hospital, submitted through the service."""
    draft = make_request(MaterialRequest.Status.DRAFT)
    return request_services.submit(principal_for(people.tech_mad), draft.pk)


class TestSubmitted:
    def test_goes_to_the_reviewers_of_the_request(
        self, people: People, submitted: MaterialRequest
    ) -> None:
        (message,) = notices()
        assert recipients_of(message) >= {people.mm_mad.pk, people.group_admin.pk}
        assert people.tech_mad.pk not in recipients_of(message)  # the author
        assert people.mm_sev.pk not in recipients_of(message)  # other branch
        assert people.partner.pk not in recipients_of(message)  # cannot review
        assert people.revoked.pk not in recipients_of(message)
        assert message.eligibility_rule == Capability.REQUEST_REVIEW
        assert message.eligibility_object_id == submitted.pk
        assert submitted.number and submitted.number in message.subject
        assert submitted.number and submitted.number in message.body
        assert "/requests/" in message.body
        assert "<html" in message.html_body

    def test_a_user_who_opted_out_is_skipped(
        self,
        people: People,
        make_request: MakeRequest,
    ) -> None:
        NotificationPreference.objects.create(user=people.mm_mad, email_opt_out=True)
        draft = make_request(MaterialRequest.Status.DRAFT)
        request_services.submit(principal_for(people.tech_mad), draft.pk)
        (message,) = notices()
        assert people.mm_mad.pk not in recipients_of(message)

    def test_a_reviewer_revoked_before_the_claim_receives_nothing(
        self, people: People, submitted: MaterialRequest
    ) -> None:
        (message,) = notices()
        before = eligibility.eligible_recipients(message)
        assert people.mm_mad in before
        Membership.objects.filter(user=people.mm_mad).update(revoked_at=timezone.now())
        assert people.mm_mad not in eligibility.eligible_recipients(message)

    def test_an_opt_out_after_queueing_is_honoured_at_the_claim(
        self, people: People, submitted: MaterialRequest
    ) -> None:
        (message,) = notices()
        NotificationPreference.objects.create(user=people.mm_mad, email_opt_out=True)
        assert people.mm_mad not in eligibility.eligible_recipients(message)

    def test_a_rolled_back_submit_leaves_no_message(
        self,
        people: People,
        make_request: MakeRequest,
    ) -> None:
        draft = make_request(MaterialRequest.Status.DRAFT)
        with pytest.raises(RuntimeError), transaction.atomic():
            request_services.submit(principal_for(people.tech_mad), draft.pk)
            raise RuntimeError("the surrounding work fails")
        assert notices() == []

    def test_fan_out_is_split_in_messages_of_twenty(
        self,
        org: Org,
        people: People,
        make_request: MakeRequest,
        member: Callable[..., User],
    ) -> None:
        for number in range(25):
            member(Role.MATERIAL_MANAGER, email=f"mm{number}@example.org", branch=org.a_mad)
        draft = make_request(MaterialRequest.Status.DRAFT)
        request_services.submit(principal_for(people.tech_mad), draft.pk)
        sizes = sorted(len(message.recipient_users) for message in notices())
        assert max(sizes) <= 20
        assert sum(sizes) >= 27


class TestDecided:
    def decide(self, people: People, submitted: MaterialRequest, **extra: Any) -> None:
        request_services.approve_all(principal_for(people.mm_mad), submitted.pk, **extra)

    def test_goes_to_the_requester_alone(self, people: People, submitted: MaterialRequest) -> None:
        EmailOutbox.objects.all().delete()
        self.decide(people, submitted)
        (message,) = notices()
        assert recipients_of(message) == {people.tech_mad.pk}
        assert message.eligibility_rule == Capability.REQUEST_VIEW
        assert submitted.number and submitted.number in message.subject
        assert "CAB-25" in message.body

    def test_a_rejection_says_so_and_carries_the_note(
        self, people: People, submitted: MaterialRequest
    ) -> None:
        EmailOutbox.objects.all().delete()
        line = submitted.lines.get()
        request_services.decide_line(
            principal_for(people.mm_mad),
            line.pk,
            decision=MaterialRequestLine.Decision.REJECTED,
            note="Out of range",
        )
        (message,) = notices()
        assert "rejected" in message.subject.lower() or "rechaz" in message.subject.lower()
        assert "Out of range" in message.body

    def test_an_opted_out_requester_gets_nothing(
        self, people: People, submitted: MaterialRequest
    ) -> None:
        NotificationPreference.objects.create(user=people.tech_mad, email_opt_out=True)
        EmailOutbox.objects.all().delete()
        self.decide(people, submitted)
        assert notices() == []

    def test_a_deactivated_requester_gets_nothing(
        self, people: People, submitted: MaterialRequest
    ) -> None:
        EmailOutbox.objects.all().delete()
        User.objects.filter(pk=people.tech_mad.pk).update(is_active=False)
        self.decide(people, submitted)
        assert notices() == []


class TestHandedOver:
    def test_a_warehouse_handover_goes_to_the_requester(
        self,
        people: People,
        make_line: MakeLine,
        hand_over: HandOver,
    ) -> None:
        line = make_line("10", sourcing=MaterialRequestLine.Sourcing.STOCK)
        handover = hand_over((line, "4"))
        (message,) = notices()
        assert recipients_of(message) == {people.tech_mad.pk}
        assert message.eligibility_object_id == line.request_id
        assert handover.number in message.body
        assert "handed over" in message.subject or "entreg" in message.subject.lower()

    def test_goods_received_on_site_go_to_the_requester_once(
        self,
        org: Org,
        people: People,
        make_line: MakeLine,
        buy: Buy,
        receive: Receive,
    ) -> None:
        line = make_line("10")
        order = buy((line, "10"))
        receive(order, "10", at=org.worksite(org.hospital))
        (message,) = notices()
        assert recipients_of(message) == {people.tech_mad.pk}
        assert "site" in message.subject or "obra" in message.subject.lower()

    def test_a_warehouse_receipt_mails_nobody(
        self,
        make_line: MakeLine,
        buy: Buy,
        receive: Receive,
    ) -> None:
        order = buy((make_line("10"), "10"))
        receive(order, "10")
        assert notices() == []

    def test_each_requester_gets_only_their_own_lines(
        self,
        org: Org,
        people: People,
        make_line: MakeLine,
        hand_over: HandOver,
        breaker: Any,
    ) -> None:
        mine = make_line("10", sourcing=MaterialRequestLine.Sourcing.STOCK)
        other = make_line("10", sourcing=MaterialRequestLine.Sourcing.STOCK, material=breaker)
        MaterialRequest.objects.filter(pk=other.request_id).update(requested_by=people.partner)
        hand_over((mine, "3"), (other, "2"))
        messages = {next(iter(m.recipient_users)): m for m in notices()}
        assert set(messages) == {people.tech_mad.pk, people.partner.pk}
        assert "CAB-25" in messages[people.tech_mad.pk].body
        assert "DIF-40" not in messages[people.tech_mad.pk].body
        assert "CAB-25" not in messages[people.partner.pk].body

    def test_a_partner_never_receives_internal_data(
        self,
        org: Org,
        people: People,
        make_line: MakeLine,
        buy: Buy,
        receive: Receive,
    ) -> None:
        line = make_line("10")
        MaterialRequest.objects.filter(pk=line.request_id).update(requested_by=people.partner)
        order = buy((line, "10"))
        receive(order, "10", at=org.worksite(org.hospital))
        (message,) = notices()
        assert recipients_of(message) == {people.partner.pk}
        text = f"{message.subject}\n{message.body}\n{message.html_body}"
        for secret in (org.supplier.name, order.number, "price", "precio"):
            assert (secret or "").lower() not in text.lower()
