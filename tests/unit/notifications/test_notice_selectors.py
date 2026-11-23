"""What the screens read of the notice settings (M15.3, M17.4): the administration page's states and
a person's, on every channel. Only a group administrator reads the first and another person's;
anyone reads their own, and nobody's but their own."""

import pytest

from apps.access.context import AccessContext
from apps.notifications import selectors
from apps.notifications.kinds import CHANNELS, NOTICES, Channel, Mode, Notice
from apps.notifications.models import (
    ChannelSwitch,
    NoticeChoice,
    NoticeDefault,
    NoticeSetting,
    NotificationPreference,
)
from apps.notifications.resolution import Reason
from tests.org import People

pytestmark = pytest.mark.django_db


def turn_off(notice: Notice, people: People, channel: Channel = Channel.EMAIL) -> None:
    NoticeDefault.objects.create(
        notice=notice, channel=channel, enabled=False, changed_by=people.group_admin
    )


def set_mode(
    people: People, user_name: str, notice: Notice, mode: Mode, channel: Channel = Channel.EMAIL
) -> None:
    NoticeSetting.objects.create(
        user=getattr(people, user_name),
        notice=notice,
        channel=channel,
        mode=mode,
        set_by=people.group_admin,
    )


class TestNoticeStates:
    def test_every_notice_in_order_all_on_with_no_own_setting(self, people: People) -> None:
        states = selectors.notice_states(AccessContext.for_user(people.group_admin))
        assert [state.spec.notice for state in states] == list(NOTICES)
        for state in states:
            assert [cell.channel for cell in state.cells] == list(Channel)
            assert all(cell.enabled and cell.own == 0 for cell in state.cells)

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
        approved = states[Notice.REQUEST_APPROVED].cell(Channel.EMAIL)
        assert (approved.enabled, approved.always, approved.never, approved.own) == (
            False,
            1,
            2,
            3,
        )
        assert approved.label == CHANNELS[Channel.EMAIL].label
        rejected = states[Notice.REQUEST_REJECTED].cell(Channel.EMAIL)
        assert rejected.enabled is True and rejected.own == 1

    def test_each_channel_is_its_own(self, people: People) -> None:
        turn_off(Notice.REQUEST_APPROVED, people, Channel.IN_APP)
        set_mode(people, "tech_mad", Notice.REQUEST_APPROVED, Mode.NEVER, Channel.IN_APP)
        states = {
            state.spec.notice: state
            for state in selectors.notice_states(AccessContext.for_user(people.group_admin))
        }
        approved = states[Notice.REQUEST_APPROVED]
        assert (approved.cell(Channel.EMAIL).enabled, approved.cell(Channel.EMAIL).own) == (True, 0)
        assert (approved.cell(Channel.IN_APP).enabled, approved.cell(Channel.IN_APP).own) == (
            False,
            1,
        )

    def test_people_who_left_are_not_counted(self, people: People) -> None:
        set_mode(people, "deactivated", Notice.REQUEST_EDITED, Mode.NEVER)
        states = selectors.notice_states(AccessContext.for_user(people.group_admin))
        assert all(cell.own == 0 for state in states for cell in state.cells)

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
            row.spec.notice: row
            for row in selectors.person_notices(AccessContext.for_user(people.group_admin), tech)
        }
        assert list(found) == list(NOTICES)
        assert [cell.channel for cell in found[Notice.REQUEST_APPROVED].cells] == list(Channel)
        approved = found[Notice.REQUEST_APPROVED].cell(Channel.EMAIL)
        assert (approved.wanted, approved.reason, approved.mode) == (
            False,
            Reason.PERSON,
            Mode.NEVER,
        )
        rejected = found[Notice.REQUEST_REJECTED].cell(Channel.EMAIL)
        assert (rejected.wanted, rejected.reason, rejected.mode) == (False, Reason.EVERYONE, None)
        edited = found[Notice.REQUEST_EDITED].cell(Channel.EMAIL)
        assert (edited.wanted, edited.reason) == (True, Reason.EVERYONE)
        # The other channel is its own: nothing set there.
        assert found[Notice.REQUEST_APPROVED].cell(Channel.IN_APP).wanted is True
        # A technician holds request.view, not request.review.
        assert found[Notice.REQUEST_APPROVED].receivable is True
        assert found[Notice.REQUEST_TO_REVIEW].receivable is False

    def test_a_reviewer_receives_the_reviewers_notifications(self, people: People) -> None:
        found = selectors.person_notices(AccessContext.for_user(people.group_admin), people.mm_mad)
        assert all(row.receivable for row in found)

    def test_their_own_switch_for_a_channel_decides_first(self, people: People) -> None:
        NotificationPreference.objects.create(user=people.mm_mad, email_opt_out=True)
        set_mode(people, "mm_mad", Notice.REQUEST_TO_REVIEW, Mode.ALWAYS)
        found = selectors.person_notices(AccessContext.for_user(people.group_admin), people.mm_mad)
        emails = [row.cell(Channel.EMAIL) for row in found]
        assert all(
            not cell.wanted and cell.off and cell.reason == Reason.CHANNEL_OFF for cell in emails
        )
        assert all(row.cell(Channel.IN_APP).wanted for row in found)

    def test_their_own_choice_and_where_it_overrides(self, people: People) -> None:
        set_mode(people, "mm_mad", Notice.REQUEST_TO_REVIEW, Mode.NEVER)
        NoticeChoice.objects.create(
            user=people.mm_mad,
            notice=Notice.REQUEST_TO_REVIEW,
            channel=Channel.EMAIL,
            enabled=True,
        )
        NoticeChoice.objects.create(
            user=people.mm_mad,
            notice=Notice.REQUEST_ASSIGNED,
            channel=Channel.EMAIL,
            enabled=True,
        )
        found = {
            row.spec.notice: row.cell(Channel.EMAIL)
            for row in selectors.person_notices(
                AccessContext.for_user(people.group_admin), people.mm_mad
            )
        }
        review = found[Notice.REQUEST_TO_REVIEW]
        assert (review.wanted, review.reason, review.choice) == (True, Reason.OWN, True)
        assert (review.mode, review.administrators, review.overrides) == (Mode.NEVER, False, True)
        assigned = found[Notice.REQUEST_ASSIGNED]
        assert assigned.reason == Reason.OWN and not assigned.overrides
        assert not found[Notice.REQUEST_APPROVED].overrides

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
        approved = next(row for row in found if row.spec.notice == Notice.REQUEST_APPROVED)
        cell = approved.cell(Channel.EMAIL)
        assert (cell.wanted, cell.reason) == (False, Reason.PERSON)

    def test_never_another_persons(self, people: People) -> None:
        set_mode(people, "mm_mad", Notice.REQUEST_TO_REVIEW, Mode.NEVER)
        found = selectors.own_notices(AccessContext.for_user(people.tech_mad))
        assert all(cell.mode is None for row in found for cell in row.cells)

    def test_nothing_without_a_signed_in_user(self) -> None:
        assert selectors.own_notices(AccessContext.anonymous()) == []


class TestOwnChannels:
    def test_every_channel_on_until_the_person_turns_it_off(self, people: People) -> None:
        ctx = AccessContext.for_user(people.tech_mad)
        states = selectors.own_channels(ctx)
        assert [state.spec.channel for state in states] == list(Channel)
        assert all(state.enabled for state in states)
        ChannelSwitch.objects.create(user=people.tech_mad, channel=Channel.IN_APP, enabled=False)
        assert [state.enabled for state in selectors.own_channels(ctx)] == [True, False]

    def test_nothing_without_a_signed_in_user(self) -> None:
        assert selectors.own_channels(AccessContext.anonymous()) == []
