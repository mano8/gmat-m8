"""What the screens read of the notice settings (M15.3): the administration page's states and a
person's. Only a group administrator reads the first and another person's; anyone reads their own,
and nobody's but their own."""

import pytest

from apps.access.context import AccessContext
from apps.notifications import selectors
from apps.notifications.kinds import NOTICES, Channel, Mode, Notice
from apps.notifications.models import NoticeDefault, NoticeSetting, NotificationPreference
from apps.notifications.resolution import Reason
from tests.org import People

pytestmark = pytest.mark.django_db


def turn_off(notice: Notice, people: People) -> None:
    NoticeDefault.objects.create(
        notice=notice, channel=Channel.EMAIL, enabled=False, changed_by=people.group_admin
    )


def set_mode(people: People, user_name: str, notice: Notice, mode: Mode) -> None:
    NoticeSetting.objects.create(
        user=getattr(people, user_name),
        notice=notice,
        channel=Channel.EMAIL,
        mode=mode,
        set_by=people.group_admin,
    )


class TestNoticeStates:
    def test_every_notice_in_order_all_on_with_no_own_setting(self, people: People) -> None:
        states = selectors.notice_states(AccessContext.for_user(people.group_admin))
        assert [state.spec.notice for state in states] == list(NOTICES)
        assert all(state.enabled and state.own == 0 for state in states)

    def test_the_state_for_everyone_and_the_people_with_their_own(self, people: People) -> None:
        turn_off(Notice.REQUEST_APPROVED, people)
        set_mode(people, "tech_mad", Notice.REQUEST_APPROVED, Mode.ALWAYS)
        set_mode(people, "mm_mad", Notice.REQUEST_APPROVED, Mode.NEVER)
        set_mode(people, "engineer", Notice.REQUEST_APPROVED, Mode.NEVER)
        set_mode(people, "mm_mad", Notice.REQUEST_REJECTED, Mode.NEVER)
        states = {
            state.spec.notice: state
            for state in selectors.notice_states(AccessContext.for_user(people.group_admin))
        }
        approved = states[Notice.REQUEST_APPROVED]
        assert (approved.enabled, approved.always, approved.never, approved.own) == (
            False,
            1,
            2,
            3,
        )
        assert states[Notice.REQUEST_REJECTED].enabled is True
        assert states[Notice.REQUEST_REJECTED].own == 1

    def test_people_who_left_are_not_counted(self, people: People) -> None:
        set_mode(people, "deactivated", Notice.REQUEST_EDITED, Mode.NEVER)
        states = selectors.notice_states(AccessContext.for_user(people.group_admin))
        assert all(state.own == 0 for state in states)

    @pytest.mark.parametrize("actor", ["mm_mad", "engineer", "wo_mad", "tech_mad", "partner"])
    def test_nothing_for_anyone_but_a_group_administrator(self, people: People, actor: str) -> None:
        turn_off(Notice.REQUEST_APPROVED, people)
        assert selectors.notice_states(AccessContext.for_user(getattr(people, actor))) == []
        assert selectors.notice_states(AccessContext.anonymous()) == []


class TestPersonNotices:
    def test_state_reason_mode_and_whether_the_roles_receive_it(self, people: People) -> None:
        tech = people.tech_mad
        turn_off(Notice.REQUEST_REJECTED, people)
        set_mode(people, "tech_mad", Notice.REQUEST_APPROVED, Mode.NEVER)
        found = {
            item.spec.notice: item
            for item in selectors.person_notices(AccessContext.for_user(people.group_admin), tech)
        }
        assert list(found) == list(NOTICES)
        approved = found[Notice.REQUEST_APPROVED]
        assert (approved.wanted, approved.reason, approved.mode) == (
            False,
            Reason.PERSON,
            Mode.NEVER,
        )
        rejected = found[Notice.REQUEST_REJECTED]
        assert (rejected.wanted, rejected.reason, rejected.mode) == (False, Reason.EVERYONE, None)
        edited = found[Notice.REQUEST_EDITED]
        assert (edited.wanted, edited.reason) == (True, Reason.EVERYONE)
        # A technician holds request.view, not request.review.
        assert found[Notice.REQUEST_APPROVED].receivable is True
        assert found[Notice.REQUEST_TO_REVIEW].receivable is False

    def test_a_reviewer_receives_the_reviewers_emails(self, people: People) -> None:
        found = selectors.person_notices(AccessContext.for_user(people.group_admin), people.mm_mad)
        assert all(item.receivable for item in found)

    def test_their_own_refusal_decides_first(self, people: People) -> None:
        NotificationPreference.objects.create(user=people.mm_mad, email_opt_out=True)
        set_mode(people, "mm_mad", Notice.REQUEST_TO_REVIEW, Mode.ALWAYS)
        found = selectors.person_notices(AccessContext.for_user(people.group_admin), people.mm_mad)
        assert all(not item.wanted and item.reason == Reason.OPTED_OUT for item in found)

    @pytest.mark.parametrize("actor", ["mm_mad", "engineer", "tech_mad", "partner"])
    def test_another_person_is_read_by_group_administrators_only(
        self, people: People, actor: str
    ) -> None:
        set_mode(people, "tech_mad", Notice.REQUEST_APPROVED, Mode.NEVER)
        ctx = AccessContext.for_user(getattr(people, actor))
        assert selectors.person_notices(ctx, people.tech_mad) == []

    def test_a_staff_account_is_never_shown(self, people: People) -> None:
        ctx = AccessContext.for_user(people.group_admin)
        assert selectors.person_notices(ctx, people.operator) == []


class TestOwnNotices:
    def test_anyone_reads_their_own(self, people: People) -> None:
        set_mode(people, "tech_mad", Notice.REQUEST_APPROVED, Mode.NEVER)
        found = selectors.own_notices(AccessContext.for_user(people.tech_mad))
        approved = next(item for item in found if item.spec.notice == Notice.REQUEST_APPROVED)
        assert (approved.wanted, approved.reason) == (False, Reason.PERSON)

    def test_never_another_persons(self, people: People) -> None:
        set_mode(people, "mm_mad", Notice.REQUEST_TO_REVIEW, Mode.NEVER)
        found = selectors.own_notices(AccessContext.for_user(people.tech_mad))
        assert all(item.mode is None for item in found)

    def test_nothing_without_a_signed_in_user(self) -> None:
        assert selectors.own_notices(AccessContext.anonymous()) == []
