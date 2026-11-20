"""Business notices in the application (M17.3, Q94, Q98): who each event tells, and when not.

Each subscriber writes the in-app row in the publisher's transaction, for the recipients the
email goes to, kept by their settings for ``Channel.IN_APP`` (Q93); a rolled-back change leaves
none; a reviewer's edits refresh the requester's unread row instead of adding another.
"""

from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.db import transaction
from django.utils import timezone

from apps.material_requests import services as request_services
from apps.material_requests.models import MaterialRequest, MaterialRequestLine
from apps.material_requests.services import LineEdit
from apps.notifications.kinds import SUBJECT, Channel, Notice
from apps.notifications.models import (
    ChannelSwitch,
    EmailOutbox,
    InAppNotification,
    Kind,
    NoticeChoice,
    NoticeDefault,
    NotificationPreference,
)
from tests.org import Org, People
from tests.support import principal_for
from tests.unit.handovers.conftest import HandOver
from tests.unit.material_requests.conftest import MakeRequest
from tests.unit.purchasing.conftest import Buy, MakeLine
from tests.unit.receiving.conftest import Receive

pytestmark = pytest.mark.django_db

D = Decimal


def rows(notice: Notice | None = None) -> list[InAppNotification]:
    found = InAppNotification.objects.order_by("notified_at", "pk")
    return list(found.filter(notice=notice) if notice else found)


def users_of(found: list[InAppNotification]) -> set[Any]:
    return {row.user_id for row in found}


@pytest.fixture
def submitted(people: People, make_request: MakeRequest) -> MaterialRequest:
    draft = make_request(MaterialRequest.Status.DRAFT)
    return request_services.submit(principal_for(people.tech_mad), draft.pk)


def approve(people: People, request: MaterialRequest) -> None:
    request_services.approve_all(principal_for(people.mm_mad), request.pk)


class TestSubmitted:
    def test_the_reviewers_are_told_and_not_the_author(
        self, people: People, submitted: MaterialRequest
    ) -> None:
        found = rows(Notice.REQUEST_TO_REVIEW)
        assert users_of(found) >= {people.mm_mad.pk, people.group_admin.pk}
        assert people.tech_mad.pk not in users_of(found)
        assert people.mm_sev.pk not in users_of(found)  # another branch
        assert people.revoked.pk not in users_of(found)
        assert all(row.object_type == SUBJECT for row in found)
        assert all(row.object_id == submitted.pk and row.read_at is None for row in found)
        (message,) = EmailOutbox.objects.filter(kind=Kind.NOTIFICATION)
        assert users_of(found) == set(message.recipient_users)  # the same recipients

    def test_the_assignee_gets_assigned_instead(
        self, people: People, make_request: MakeRequest
    ) -> None:
        draft = make_request(MaterialRequest.Status.DRAFT)
        request_services.submit(
            principal_for(people.tech_mad), draft.pk, reviewer_id=people.engineer.pk
        )
        assert users_of(rows(Notice.REQUEST_ASSIGNED)) == {people.engineer.pk}
        assert people.engineer.pk not in users_of(rows(Notice.REQUEST_TO_REVIEW))

    def test_a_rolled_back_submit_leaves_no_row(
        self, people: People, make_request: MakeRequest
    ) -> None:
        draft = make_request(MaterialRequest.Status.DRAFT)
        with pytest.raises(RuntimeError), transaction.atomic():
            request_services.submit(principal_for(people.tech_mad), draft.pk)
            raise RuntimeError("the surrounding work fails")
        assert rows() == []


class TestWhoWantsIt:
    """Q93 on the in-app channel, independent of email."""

    def test_the_requester_is_told_of_the_approval(
        self, people: People, submitted: MaterialRequest
    ) -> None:
        InAppNotification.objects.all().delete()
        approve(people, submitted)
        (row,) = rows()
        assert (row.user_id, row.notice) == (people.tech_mad.pk, Notice.REQUEST_APPROVED)

    def test_no_business_email_does_not_stop_the_application(
        self, people: People, submitted: MaterialRequest
    ) -> None:
        NotificationPreference.objects.create(user=people.tech_mad, email_opt_out=True)
        approve(people, submitted)
        assert users_of(rows(Notice.REQUEST_APPROVED)) == {people.tech_mad.pk}
        assert not EmailOutbox.objects.filter(notice=Notice.REQUEST_APPROVED).exists()

    def test_the_channel_turned_off_stops_it_and_email_goes_on(
        self, people: People, submitted: MaterialRequest
    ) -> None:
        ChannelSwitch.objects.create(user=people.tech_mad, channel=Channel.IN_APP, enabled=False)
        approve(people, submitted)
        assert rows(Notice.REQUEST_APPROVED) == []
        assert EmailOutbox.objects.filter(notice=Notice.REQUEST_APPROVED).exists()

    def test_an_own_choice_off_stops_that_notice_only(
        self, people: People, submitted: MaterialRequest
    ) -> None:
        NoticeChoice.objects.create(
            user=people.tech_mad,
            notice=Notice.REQUEST_APPROVED,
            channel=Channel.IN_APP,
            enabled=False,
        )
        approve(people, submitted)
        assert rows(Notice.REQUEST_APPROVED) == []

    def test_off_for_everyone_stops_it(self, people: People, submitted: MaterialRequest) -> None:
        off_for_everyone(people, Notice.REQUEST_APPROVED)
        approve(people, submitted)
        assert rows(Notice.REQUEST_APPROVED) == []

    def test_an_own_choice_on_wins_over_off_for_everyone(
        self, people: People, submitted: MaterialRequest
    ) -> None:
        off_for_everyone(people, Notice.REQUEST_APPROVED)
        NoticeChoice.objects.create(
            user=people.tech_mad,
            notice=Notice.REQUEST_APPROVED,
            channel=Channel.IN_APP,
            enabled=True,
        )
        approve(people, submitted)
        assert users_of(rows(Notice.REQUEST_APPROVED)) == {people.tech_mad.pk}


def off_for_everyone(people: People, notice: Notice) -> None:
    NoticeDefault.objects.create(
        notice=notice, channel=Channel.IN_APP, enabled=False, changed_by=people.group_admin
    )


@pytest.fixture
def approved(people: People, make_request: MakeRequest) -> MaterialRequest:
    request = request_services.submit(principal_for(people.tech_mad), make_request(lines=2).pk)
    approve(people, request)
    InAppNotification.objects.all().delete()
    return request


def change(people: People, request: MaterialRequest, line_no: int, quantity: int) -> None:
    line = MaterialRequestLine.objects.get(request=request, line_no=line_no)
    request_services.edit_line(
        principal_for(people.engineer), line.pk, LineEdit("Stock", {"requested_qty": D(quantity)})
    )


class TestEdited:
    def test_edits_refresh_one_unread_row(self, people: People, approved: MaterialRequest) -> None:
        change(people, approved, 1, 12)
        change(people, approved, 2, 11)
        (row,) = rows(Notice.REQUEST_EDITED)
        assert (row.user_id, row.object_id) == (people.tech_mad.pk, approved.pk)

    def test_once_read_the_next_edit_adds_a_row(
        self, people: People, approved: MaterialRequest
    ) -> None:
        change(people, approved, 1, 12)
        InAppNotification.objects.update(read_at=timezone.now())
        change(people, approved, 2, 11)
        found = rows(Notice.REQUEST_EDITED)
        assert len(found) == 2
        assert [row.read_at is None for row in found].count(True) == 1

    def test_the_row_is_refreshed_even_while_the_email_waits(
        self, people: People, approved: MaterialRequest
    ) -> None:
        change(people, approved, 1, 12)
        InAppNotification.objects.update(notified_at=timezone.now() - timedelta(hours=1))
        change(people, approved, 2, 11)
        (row,) = rows(Notice.REQUEST_EDITED)
        assert row.notified_at > timezone.now() - timedelta(minutes=5)
        assert EmailOutbox.objects.filter(notice=Notice.REQUEST_EDITED).count() == 1

    def test_the_requester_is_not_told_of_their_own_edits(
        self, people: People, approved: MaterialRequest
    ) -> None:
        MaterialRequest.objects.filter(pk=approved.pk).update(requested_by=people.engineer)
        change(people, approved, 1, 12)
        assert rows(Notice.REQUEST_EDITED) == []


class TestHandedOver:
    def test_a_handover_tells_the_requester(
        self, people: People, make_line: MakeLine, hand_over: HandOver
    ) -> None:
        line = make_line("10", sourcing=MaterialRequestLine.Sourcing.STOCK)
        InAppNotification.objects.all().delete()
        hand_over((line, "4"))
        (row,) = rows()
        assert (row.user_id, row.notice) == (people.tech_mad.pk, Notice.REQUEST_HANDED_OVER)
        assert row.object_id == line.request_id

    def test_goods_received_on_site_tell_the_requester_once(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy, receive: Receive
    ) -> None:
        line = make_line("10")
        order = buy((line, "10"))
        InAppNotification.objects.all().delete()
        receive(order, "10", at=org.worksite(org.hospital))
        (row,) = rows()
        assert (row.user_id, row.notice) == (people.tech_mad.pk, Notice.REQUEST_HANDED_OVER)
