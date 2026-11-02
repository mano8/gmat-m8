"""The requester's notice of reviewer edits (M7b.3, Q34): one message per batch of edits.

The first edit queues a message held back ``EDIT_BATCH``; the edits made while it waits
are covered by it (it only says that the request changed, with the link). Once the worker
has claimed it, the next edit queues another. The requester is not told of their own
edits, and the worker checks ``request.view`` again at the claim.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import transaction
from django.db.models.functions import Now
from django.utils import timezone

from apps.access.principals import SystemPrincipal
from apps.access.roles import Capability
from apps.core.db import db_now
from apps.material_requests import services
from apps.material_requests.models import MaterialRequest, MaterialRequestLine
from apps.material_requests.services import LineEdit
from apps.notifications import eligibility, notices, outbox
from apps.notifications.models import EmailOutbox, Kind, NotificationPreference, Status
from apps.notifications.services import Claim, claim_next
from apps.organizations.models import Membership
from tests.org import People
from tests.support import principal_for
from tests.unit.material_requests.conftest import MakeRequest

pytestmark = pytest.mark.django_db

D = Decimal


def edited_notices() -> list[EmailOutbox]:
    return list(
        EmailOutbox.objects.filter(
            kind=Kind.NOTIFICATION, source_ref__startswith="request.edited:"
        ).order_by("created_at")
    )


@pytest.fixture
def approved(people: People, make_request: MakeRequest) -> MaterialRequest:
    """A hospital request of A-MAD's technician, submitted and approved."""
    tech, mm = principal_for(people.tech_mad), principal_for(people.mm_mad)
    request = services.submit(tech, make_request(lines=2).pk)
    services.approve_all(mm, request.pk)
    EmailOutbox.objects.all().delete()
    return request


def change(people: People, request: MaterialRequest, line_no: int, quantity: int) -> None:
    line = MaterialRequestLine.objects.get(request=request, line_no=line_no)
    services.edit_line(
        principal_for(people.engineer), line.pk, LineEdit("Stock", {"requested_qty": D(quantity)})
    )


class TestEdited:
    def test_the_requester_is_told_once_per_batch(
        self, people: People, approved: MaterialRequest
    ) -> None:
        before = db_now()
        change(people, approved, 1, 12)
        change(people, approved, 2, 11)
        services.remove_line_as_reviewer(
            principal_for(people.mm_mad),
            MaterialRequestLine.objects.get(request=approved, line_no=2).pk,
            reason="Not needed",
        )
        (message,) = edited_notices()
        assert set(message.recipient_users) == {people.tech_mad.pk}
        assert message.eligibility_rule == Capability.REQUEST_VIEW
        assert message.eligibility_object_id == approved.pk
        assert message.source_ref == f"request.edited:{approved.pk}"
        assert message.not_before >= before + notices.EDIT_BATCH
        assert approved.number and approved.number in message.subject
        assert f"/requests/{approved.pk}/" in message.body
        assert "<html" in message.html_body
        assert "Stock" not in message.body  # what changed is on the page, not in the mail

    def test_the_worker_waits_for_the_batch(
        self, people: People, approved: MaterialRequest
    ) -> None:
        change(people, approved, 1, 12)
        assert claim_next(SystemPrincipal(), worker="test") is None
        EmailOutbox.objects.update(not_before=Now())
        claimed = claim_next(SystemPrincipal(), worker="test")
        assert isinstance(claimed, Claim)
        assert set(claimed.to + claimed.bcc) == {people.tech_mad.email}

    def test_an_edit_after_the_claim_starts_a_new_batch(
        self, people: People, approved: MaterialRequest
    ) -> None:
        change(people, approved, 1, 12)
        EmailOutbox.objects.update(not_before=Now())
        assert isinstance(claim_next(SystemPrincipal(), worker="test"), Claim)
        change(people, approved, 1, 13)
        first, second = edited_notices()
        assert (first.status, second.status) == (Status.SENDING, Status.QUEUED)

    def test_not_for_their_own_edits(self, people: People, make_request: MakeRequest) -> None:
        engineer = principal_for(people.engineer)
        own = services.submit(engineer, make_request(user=people.engineer).pk)
        line = own.lines.get()
        services.edit_line(engineer, line.pk, LineEdit("Mine", {"requested_qty": D(3)}))
        assert edited_notices() == []

    def test_an_opted_out_requester_gets_nothing(
        self, people: People, approved: MaterialRequest
    ) -> None:
        NotificationPreference.objects.create(user=people.tech_mad, email_opt_out=True)
        change(people, approved, 1, 12)
        assert edited_notices() == []

    def test_a_requester_revoked_before_the_claim_receives_nothing(
        self, people: People, approved: MaterialRequest
    ) -> None:
        change(people, approved, 1, 12)
        (message,) = edited_notices()
        assert people.tech_mad in eligibility.eligible_recipients(message)
        Membership.objects.filter(user=people.tech_mad).update(revoked_at=timezone.now())
        assert people.tech_mad not in eligibility.eligible_recipients(message)

    def test_a_rolled_back_edit_leaves_no_message(
        self, people: People, approved: MaterialRequest
    ) -> None:
        with pytest.raises(RuntimeError), transaction.atomic():
            change(people, approved, 1, 12)
            raise RuntimeError("the surrounding work fails")
        assert edited_notices() == []


class TestOutboxBatching:
    def test_waiting_sees_only_unclaimed_notifications(
        self, people: People, approved: MaterialRequest
    ) -> None:
        with transaction.atomic():
            assert not outbox.waiting("request.edited:x")
            (message,) = outbox.queue_notification(
                recipients=[people.tech_mad.pk],
                capability=Capability.REQUEST_VIEW,
                obj=approved,
                subject="s",
                body="b",
                source_ref="request.edited:x",
            )
            assert outbox.waiting("request.edited:x")
            outbox.defer([message], db_now() + timedelta(minutes=5))
        message.refresh_from_db()
        assert message.not_before > timezone.now()
        EmailOutbox.objects.update(status=Status.SENT)
        with transaction.atomic():
            assert not outbox.waiting("request.edited:x")

    @pytest.mark.django_db(transaction=True)
    def test_both_need_the_callers_transaction(self) -> None:
        with pytest.raises(outbox.OutboxOutsideTransaction):
            outbox.waiting("x")
        with pytest.raises(outbox.OutboxOutsideTransaction):
            outbox.defer([], timezone.now())
