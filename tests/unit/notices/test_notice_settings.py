"""Each business email names its notice, and the settings decide who it is queued for (M15.1).

The subscribers queue through ``queue_notification``, which keeps only the recipients who
want the notice (``apps.notifications.resolution``): their refusal of business email wins,
then their own setting, then the setting for everyone. Shown here with the A/B/C people.
"""

from decimal import Decimal

import pytest

from apps.accounts.models import User
from apps.material_requests import services as request_services
from apps.material_requests.models import MaterialRequest, MaterialRequestLine
from apps.material_requests.services import LineEdit
from apps.notifications.kinds import NOTICES, Channel, Mode, Notice
from apps.notifications.models import (
    EmailOutbox,
    Kind,
    NoticeDefault,
    NoticeSetting,
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


def queued() -> list[EmailOutbox]:
    return list(EmailOutbox.objects.filter(kind=Kind.NOTIFICATION).order_by("created_at"))


def by_notice() -> dict[str, EmailOutbox]:
    messages = queued()
    found = {message.notice: message for message in messages}
    assert len(found) == len(messages), "one message per notice in these scenarios"
    return found


def assert_named(message: EmailOutbox, notice: Notice) -> None:
    assert message.notice == notice
    assert message.eligibility_rule == NOTICES[notice].capability


def turn_off(notice: Notice, by: User) -> None:
    NoticeDefault.objects.create(notice=notice, channel=Channel.EMAIL, enabled=False, changed_by=by)


def set_mode(user: User, notice: Notice, mode: Mode, by: User) -> None:
    NoticeSetting.objects.create(
        user=user, notice=notice, channel=Channel.EMAIL, mode=mode, set_by=by
    )


@pytest.fixture
def draft(make_request: MakeRequest) -> MaterialRequest:
    return make_request(MaterialRequest.Status.DRAFT)


@pytest.fixture
def submitted(people: People, draft: MaterialRequest) -> MaterialRequest:
    request = request_services.submit(principal_for(people.tech_mad), draft.pk)
    EmailOutbox.objects.all().delete()
    return request


class TestEachEmailNamesItsNotice:
    def test_submit_without_and_with_an_assigned_reviewer(
        self, people: People, make_request: MakeRequest
    ) -> None:
        tech = principal_for(people.tech_mad)
        request_services.submit(tech, make_request(MaterialRequest.Status.DRAFT).pk)
        (general,) = queued()
        assert_named(general, Notice.REQUEST_TO_REVIEW)
        EmailOutbox.objects.all().delete()
        request_services.submit(
            tech, make_request(MaterialRequest.Status.DRAFT).pk, reviewer_id=people.engineer.pk
        )
        found = by_notice()
        assert set(found) == {Notice.REQUEST_TO_REVIEW, Notice.REQUEST_ASSIGNED}
        assert_named(found[Notice.REQUEST_TO_REVIEW], Notice.REQUEST_TO_REVIEW)
        assert_named(found[Notice.REQUEST_ASSIGNED], Notice.REQUEST_ASSIGNED)
        assert found[Notice.REQUEST_ASSIGNED].recipient_users == [people.engineer.pk]

    def test_a_reassignment(self, people: People, submitted: MaterialRequest) -> None:
        request_services.assign_reviewer(
            principal_for(people.mm_mad), submitted.pk, reviewer_id=people.engineer.pk, note="X"
        )
        (message,) = queued()
        assert_named(message, Notice.REQUEST_ASSIGNED)

    def test_an_approval(self, people: People, submitted: MaterialRequest) -> None:
        request_services.approve_all(principal_for(people.mm_mad), submitted.pk)
        (message,) = queued()
        assert_named(message, Notice.REQUEST_APPROVED)

    def test_a_rejection(self, people: People, submitted: MaterialRequest) -> None:
        request_services.decide_line(
            principal_for(people.mm_mad),
            submitted.lines.get().pk,
            decision=MaterialRequestLine.Decision.REJECTED,
            note="No",
        )
        (message,) = queued()
        assert_named(message, Notice.REQUEST_REJECTED)

    def test_a_reviewers_edit(self, people: People, submitted: MaterialRequest) -> None:
        request_services.edit_line(
            principal_for(people.engineer),
            submitted.lines.get().pk,
            LineEdit("More", {"requested_qty": D(3)}),
        )
        (message,) = queued()
        assert_named(message, Notice.REQUEST_EDITED)

    def test_a_warehouse_handover(self, make_line: MakeLine, hand_over: HandOver) -> None:
        hand_over((make_line("10", sourcing=MaterialRequestLine.Sourcing.STOCK), "4"))
        (message,) = queued()
        assert_named(message, Notice.REQUEST_HANDED_OVER)

    def test_goods_received_on_site(
        self, org: Org, make_line: MakeLine, buy: Buy, receive: Receive
    ) -> None:
        order = buy((make_line("10"), "10"))
        receive(order, "10", at=org.worksite(org.hospital))
        (message,) = queued()
        assert_named(message, Notice.REQUEST_HANDED_OVER)


class TestSettingsAtQueueing:
    def test_off_for_everyone_nothing_is_queued(
        self, people: People, draft: MaterialRequest
    ) -> None:
        turn_off(Notice.REQUEST_TO_REVIEW, people.group_admin)
        request_services.submit(principal_for(people.tech_mad), draft.pk)
        assert queued() == []

    def test_off_for_everyone_and_always_for_one_reviewer(
        self, people: People, draft: MaterialRequest
    ) -> None:
        turn_off(Notice.REQUEST_TO_REVIEW, people.group_admin)
        set_mode(people.engineer, Notice.REQUEST_TO_REVIEW, Mode.ALWAYS, people.group_admin)
        request_services.submit(principal_for(people.tech_mad), draft.pk)
        (message,) = queued()
        assert message.recipient_users == [people.engineer.pk]

    def test_never_for_one_reviewer_leaves_the_others(
        self, people: People, draft: MaterialRequest
    ) -> None:
        set_mode(people.group_admin, Notice.REQUEST_TO_REVIEW, Mode.NEVER, people.group_admin)
        request_services.submit(principal_for(people.tech_mad), draft.pk)
        (message,) = queued()
        assert people.group_admin.pk not in message.recipient_users
        assert {people.mm_mad.pk, people.engineer.pk} <= set(message.recipient_users)

    def test_the_persons_refusal_wins_over_always(
        self, people: People, draft: MaterialRequest
    ) -> None:
        turn_off(Notice.REQUEST_TO_REVIEW, people.group_admin)
        set_mode(people.engineer, Notice.REQUEST_TO_REVIEW, Mode.ALWAYS, people.group_admin)
        NotificationPreference.objects.create(user=people.engineer, email_opt_out=True)
        request_services.submit(principal_for(people.tech_mad), draft.pk)
        assert queued() == []

    def test_a_setting_never_reaches_who_cannot_receive_the_email(
        self, people: People, draft: MaterialRequest
    ) -> None:
        """``ALWAYS`` decides between wanting and not; it never adds a recipient."""
        set_mode(people.mm_sev, Notice.REQUEST_TO_REVIEW, Mode.ALWAYS, people.group_admin)
        set_mode(people.partner, Notice.REQUEST_TO_REVIEW, Mode.ALWAYS, people.group_admin)
        request_services.submit(principal_for(people.tech_mad), draft.pk)
        (message,) = queued()
        assert people.mm_sev.pk not in message.recipient_users  # another branch
        assert people.partner.pk not in message.recipient_users  # cannot review

    def test_the_assigned_reviewer_follows_the_assigned_notice(
        self, people: People, draft: MaterialRequest
    ) -> None:
        """Assigned and to review are two notices: the assignee never gets the general one."""
        set_mode(people.engineer, Notice.REQUEST_ASSIGNED, Mode.NEVER, people.group_admin)
        request_services.submit(
            principal_for(people.tech_mad), draft.pk, reviewer_id=people.engineer.pk
        )
        (general,) = queued()
        assert general.notice == Notice.REQUEST_TO_REVIEW
        assert people.engineer.pk not in general.recipient_users

    def test_approved_and_rejected_are_separate(
        self, people: People, make_request: MakeRequest
    ) -> None:
        """A requester may keep only the rejections (Q88)."""
        set_mode(people.tech_mad, Notice.REQUEST_APPROVED, Mode.NEVER, people.group_admin)
        tech, mm = principal_for(people.tech_mad), principal_for(people.mm_mad)
        approved = request_services.submit(tech, make_request(MaterialRequest.Status.DRAFT).pk)
        rejected = request_services.submit(tech, make_request(MaterialRequest.Status.DRAFT).pk)
        EmailOutbox.objects.all().delete()
        request_services.approve_all(mm, approved.pk)
        assert queued() == []
        request_services.decide_line(
            mm,
            rejected.lines.get().pk,
            decision=MaterialRequestLine.Decision.REJECTED,
            note="No",
        )
        (message,) = queued()
        assert message.notice == Notice.REQUEST_REJECTED
        assert message.recipient_users == [people.tech_mad.pk]

    def test_the_handover_follows_the_requesters_setting(
        self, people: People, make_line: MakeLine, hand_over: HandOver
    ) -> None:
        turn_off(Notice.REQUEST_HANDED_OVER, people.group_admin)
        hand_over((make_line("10", sourcing=MaterialRequestLine.Sourcing.STOCK), "4"))
        assert queued() == []
