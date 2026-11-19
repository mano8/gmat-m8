"""Business notices (M15.1, M17.1): the registries, who wants a notice, and the outbox that
follows it.

The four levels of Q93, per channel: the person's switch for the channel wins, then their own
choice, then an administrator's setting for them, then the setting for everyone (no row: the
channel's default). Applied when an email is queued and again at the claim.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from django.db import IntegrityError, transaction
from django.test import override_settings

from apps.access.principals import SystemPrincipal
from apps.access.roles import Capability
from apps.accounts.models import User
from apps.notifications import eligibility, resolution
from apps.notifications.kinds import (
    CHANNELS,
    NOTICES,
    Channel,
    ChannelSpec,
    Mode,
    Notice,
    NoticeSpec,
    _channels,
    _registry,
)
from apps.notifications.models import (
    ChannelSwitch,
    EmailOutbox,
    Kind,
    NoticeChoice,
    NoticeDefault,
    NoticeSetting,
    NotificationPreference,
    Status,
)
from apps.notifications.outbox import queue_notification
from apps.notifications.worker import Worker, send
from tests.org import Org, People
from tests.smtp import fake_smtp, smtp_mailers

pytestmark = pytest.mark.django_db

NOTICE = Notice.REQUEST_TO_REVIEW


def set_default(notice: Notice, enabled: bool, by: User, channel: Channel = Channel.EMAIL) -> None:
    NoticeDefault.objects.update_or_create(
        notice=notice, channel=channel, defaults={"enabled": enabled, "changed_by": by}
    )


def set_mode(
    user: User, notice: Notice, mode: Mode, by: User, channel: Channel = Channel.EMAIL
) -> None:
    NoticeSetting.objects.update_or_create(
        user=user, notice=notice, channel=channel, defaults={"mode": mode, "set_by": by}
    )


def choose(user: User, notice: Notice, enabled: bool, channel: Channel = Channel.EMAIL) -> None:
    """The person's own choice (M17.1)."""
    NoticeChoice.objects.update_or_create(
        user=user, notice=notice, channel=channel, defaults={"enabled": enabled}
    )


def switch(user: User, channel: Channel, enabled: bool) -> None:
    """The person's switch for ``channel``: email's is their *no business email* (M7.1)."""
    if channel == Channel.EMAIL:
        NotificationPreference.objects.update_or_create(
            user=user, defaults={"email_opt_out": not enabled}
        )
    else:
        ChannelSwitch.objects.update_or_create(
            user=user, channel=channel, defaults={"enabled": enabled}
        )


def refuse(user: User) -> None:
    switch(user, Channel.EMAIL, False)


def a_worker(tmp_path: Path) -> Worker:
    """The worker process's loop, sending through the configured ``outbound`` mailer."""
    flags = tmp_path / "flags"
    flags.mkdir()
    return Worker(
        principal=SystemPrincipal(),
        flags_dir=flags,
        heartbeat_file=tmp_path / "heartbeat",
        outbound=True,
        poll_interval=0.01,
        reconcile_interval=60,
        housekeeping_interval=3600,
        name="test-worker",
        send=send,
    )


class TestRegistry:
    def test_every_notice_once_in_the_order_of_the_screens(self) -> None:
        assert list(NOTICES) == list(Notice)
        orders = [spec.order for spec in NOTICES.values()]
        assert orders == sorted(orders) and len(set(orders)) == len(orders)
        for notice, spec in NOTICES.items():
            assert spec.notice is notice
            assert spec.label == str(notice.label)
            assert str(spec.recipients) and str(spec.when)

    def test_reviewers_receive_through_review_and_requesters_through_view(self) -> None:
        review = {Notice.REQUEST_TO_REVIEW, Notice.REQUEST_ASSIGNED}
        for notice, spec in NOTICES.items():
            expected = Capability.REQUEST_REVIEW if notice in review else Capability.REQUEST_VIEW
            assert spec.capability == expected

    def test_a_missing_or_repeated_notice_is_refused(self) -> None:
        specs = list(NOTICES.values())
        with pytest.raises(ValueError, match="exactly one entry"):
            _registry(*specs[1:])
        with pytest.raises(ValueError, match="exactly one entry"):
            _registry(*specs, specs[0])

    def test_the_channels_and_modes(self) -> None:
        """Email and in the application, both on by default, neither opt-in (Q95, Q96); nothing
        of the Telegram bot until its plan is done."""
        assert list(Channel) == [Channel.EMAIL, Channel.IN_APP]
        assert list(CHANNELS) == list(Channel)
        for channel, spec in CHANNELS.items():
            assert spec.channel is channel
            assert spec.label == str(channel.label)
            assert (spec.default, spec.opt_in) == (True, False)
        assert set(Mode) == {Mode.ALWAYS, Mode.NEVER}

    def test_a_missing_or_repeated_channel_is_refused(self) -> None:
        specs = list(CHANNELS.values())
        with pytest.raises(ValueError, match="exactly one entry"):
            _channels(*specs[1:])
        with pytest.raises(ValueError, match="exactly one entry"):
            _channels(*specs, specs[0])

    def test_a_spec_is_frozen(self) -> None:
        spec = NOTICES[NOTICE]
        with pytest.raises(AttributeError):
            spec.order = 1  # type: ignore[misc]
        assert isinstance(spec, NoticeSpec)


# The channel's switch (False: off), the person's own choice, an administrator's setting for
# them, the setting for everyone (None: no row), on each channel → wanted (Q93).
TABLE = [
    (
        channel,
        on,
        choice,
        mode,
        default,
        on
        and (
            choice
            if choice is not None
            else mode == Mode.ALWAYS
            if mode is not None
            else default is not False
        ),
    )
    for channel in Channel
    for on in (True, False)
    for choice in (None, True, False)
    for mode in (None, Mode.ALWAYS, Mode.NEVER)
    for default in (None, True, False)
]
TABLE_IDS = [f"{c}-on={o}-choice={ch}-mode={m}-default={d}" for c, o, ch, m, d, _ in TABLE]


def arrange(
    person: User,
    admin: User,
    channel: Channel,
    on: bool,
    choice: bool | None,
    mode: Mode | None,
    default: bool | None,
) -> None:
    """The four levels for ``NOTICE`` on ``channel``; the other channel is set the other way
    round, so a level of one channel that counted on the other would show."""
    other = next(c for c in Channel if c != channel)
    switch(person, other, not on)
    if not on:
        switch(person, channel, False)
    if choice is not None:
        choose(person, NOTICE, choice, channel)
        choose(person, NOTICE, not choice, other)
    if mode is not None:
        set_mode(person, NOTICE, mode, admin, channel)
    if default is not None:
        set_default(NOTICE, default, admin, channel)
        set_default(NOTICE, not default, admin, other)


def level(on: bool, choice: bool | None, mode: Mode | None) -> resolution.Reason:
    if not on:
        return resolution.Reason.CHANNEL_OFF
    if choice is not None:
        return resolution.Reason.OWN
    return resolution.Reason.PERSON if mode is not None else resolution.Reason.EVERYONE


class TestResolution:
    @pytest.mark.parametrize(
        ("channel", "on", "choice", "mode", "default", "expected"), TABLE, ids=TABLE_IDS
    )
    def test_the_four_levels_on_every_channel(
        self,
        people: People,
        channel: Channel,
        on: bool,
        choice: bool | None,
        mode: Mode | None,
        default: bool | None,
        expected: bool,
    ) -> None:
        person, admin = people.mm_mad, people.group_admin
        arrange(person, admin, channel, on, choice, mode, default)
        assert (person.pk in resolution.wanted([person.pk], NOTICE, channel)) is expected

    @pytest.mark.parametrize(
        ("channel", "on", "choice", "mode", "default", "expected"), TABLE, ids=TABLE_IDS
    )
    def test_decisions_say_the_same_with_the_level_that_decided(
        self,
        people: People,
        channel: Channel,
        on: bool,
        choice: bool | None,
        mode: Mode | None,
        default: bool | None,
        expected: bool,
    ) -> None:
        """The screens' reading (M15.3, M17.4) never differs from the rule queueing and the
        claim apply."""
        person, admin = people.mm_mad, people.group_admin
        arrange(person, admin, channel, on, choice, mode, default)
        decided = resolution.decisions(person.pk, channel)
        assert set(decided) == set(Notice)
        found = decided[NOTICE]
        wanted = person.pk in resolution.wanted([person.pk], NOTICE, channel)
        assert found.wanted is expected is wanted
        assert (found.choice, found.mode) == (choice, mode)
        assert found.reason == level(on, choice, mode)
        administrators = mode == Mode.ALWAYS if mode is not None else default is not False
        assert found.administrators is administrators
        # A notice nobody touched follows the channel's default, which is on.
        untouched = decided[Notice.REQUEST_EDITED]
        assert (untouched.wanted, untouched.administrators) == (on, True)

    def test_an_opt_in_channel_is_off_until_the_person_turns_it_on(
        self, people: People, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No channel is opt-in yet; the Telegram bot's will be (Q96): its switch is off with no
        row, and only the person's own *on* opens it."""
        spec = CHANNELS[Channel.IN_APP]
        opt_in = ChannelSpec(spec.channel, default=True, opt_in=True, order=spec.order)
        monkeypatch.setattr(resolution, "CHANNELS", dict(CHANNELS) | {Channel.IN_APP: opt_in})
        person = people.mm_mad
        assert resolution.wanted([person.pk], NOTICE, Channel.IN_APP) == set()
        assert resolution.decisions(person.pk, Channel.IN_APP)[NOTICE].reason == (
            resolution.Reason.CHANNEL_OFF
        )
        switch(person, Channel.IN_APP, False)
        assert resolution.wanted([person.pk], NOTICE, Channel.IN_APP) == set()
        switch(person, Channel.IN_APP, True)
        assert resolution.wanted([person.pk], NOTICE, Channel.IN_APP) == {person.pk}
        assert resolution.wanted([person.pk], NOTICE) == {person.pk}  # email is not opt-in

    def test_turned_off_answers_for_every_channel(self, people: People) -> None:
        ids = [people.mm_mad.pk, people.engineer.pk, people.tech_mad.pk]
        switch(people.mm_mad, Channel.EMAIL, False)
        switch(people.engineer, Channel.IN_APP, False)
        switch(people.tech_mad, Channel.IN_APP, True)
        for channel, off in (
            (Channel.EMAIL, {people.mm_mad.pk}),
            (Channel.IN_APP, {people.engineer.pk}),
        ):
            found = User.objects.filter(pk__in=ids).filter(resolution.turned_off(channel))
            assert set(found.values_list("pk", flat=True)) == off

    def test_settings_of_another_notice_or_person_do_not_count(self, people: People) -> None:
        admin, person, other = people.group_admin, people.mm_mad, people.engineer
        set_default(Notice.REQUEST_APPROVED, False, admin)
        set_mode(other, NOTICE, Mode.NEVER, admin)
        set_mode(person, Notice.REQUEST_REJECTED, Mode.NEVER, admin)
        assert resolution.wanted([person.pk, other.pk], NOTICE) == {person.pk}
        assert resolution.wanted([person.pk, other.pk], Notice.REQUEST_APPROVED) == set()

    def test_one_query_for_many_people(
        self, people: People, django_assert_num_queries: Any
    ) -> None:
        ids = [user.pk for user in people.all().values()]
        set_default(NOTICE, False, people.group_admin)
        set_mode(people.mm_mad, NOTICE, Mode.ALWAYS, people.group_admin)
        with django_assert_num_queries(1):
            assert resolution.wanted(ids, NOTICE) == {people.mm_mad.pk}

    def test_nobody_asked_costs_no_query(self, django_assert_num_queries: Any) -> None:
        with django_assert_num_queries(0):
            assert resolution.wanted([], NOTICE) == set()

    def test_without_a_notice_only_the_switch_counts(self, people: People) -> None:
        """A message queued before M15: the settings of no notice apply to it."""
        admin, person, other = people.group_admin, people.mm_mad, people.engineer
        for notice in Notice:
            set_default(notice, False, admin)
            set_mode(person, notice, Mode.NEVER, admin)
            choose(person, notice, False)
        refuse(other)
        assert resolution.wanted([person.pk, other.pk], "") == {person.pk}

    def test_one_query_with_every_level_set(
        self, people: People, django_assert_num_queries: Any
    ) -> None:
        ids = [user.pk for user in people.all().values()]
        admin = people.group_admin
        set_default(NOTICE, False, admin, Channel.IN_APP)
        set_mode(people.engineer, NOTICE, Mode.ALWAYS, admin, Channel.IN_APP)
        set_mode(people.mm_mad, NOTICE, Mode.NEVER, admin, Channel.IN_APP)
        choose(people.mm_mad, NOTICE, True, Channel.IN_APP)
        choose(people.engineer, NOTICE, False, Channel.IN_APP)
        choose(people.tech_mad, NOTICE, True, Channel.IN_APP)
        switch(people.tech_mad, Channel.IN_APP, False)
        with django_assert_num_queries(1):
            assert resolution.wanted(ids, NOTICE, Channel.IN_APP) == {people.mm_mad.pk}

    def test_decisions_cost_four_queries(
        self, people: People, django_assert_num_queries: Any
    ) -> None:
        with django_assert_num_queries(4):
            resolution.decisions(people.mm_mad.pk, Channel.IN_APP)

    def test_filter_wanted_keeps_the_callers_conditions(self, people: People) -> None:
        users = User.objects.filter(pk__in=[people.mm_mad.pk, people.engineer.pk]).exclude(
            pk=people.engineer.pk
        )
        assert list(resolution.filter_wanted(users, NOTICE)) == [people.mm_mad]


class TestOutboxRow:
    def test_a_notification_records_its_notice(
        self, people: People, notification: Callable[..., EmailOutbox]
    ) -> None:
        message = notification(people.mm_mad, notice=Notice.REQUEST_HANDED_OVER)
        assert message.notice == Notice.REQUEST_HANDED_OVER

    def test_an_unknown_notice_is_refused(self, org: Org, people: People) -> None:
        with transaction.atomic(), pytest.raises(ValueError, match="Unknown notice"):
            queue_notification(
                notice="NO_SUCH",  # type: ignore[arg-type]
                recipients=[people.mm_mad.pk],
                capability=Capability.PROJECT_VIEW,
                obj=org.hospital,
                subject="s",
                body="b",
            )

    def test_nobody_who_wants_it_queues_nothing(self, org: Org, people: People) -> None:
        set_default(NOTICE, False, people.group_admin)
        refuse(people.engineer)
        set_mode(people.engineer, NOTICE, Mode.ALWAYS, people.group_admin)
        with transaction.atomic():
            queued = queue_notification(
                notice=NOTICE,
                recipients=[people.mm_mad.pk, people.engineer.pk],
                capability=Capability.PROJECT_VIEW,
                obj=org.hospital,
                subject="s",
                body="b",
            )
        assert queued == []
        assert not EmailOutbox.objects.exists()

    def test_only_those_who_want_it_are_recipients(
        self, people: People, notification: Callable[..., EmailOutbox]
    ) -> None:
        set_default(NOTICE, False, people.group_admin)
        set_mode(people.mm_mad, NOTICE, Mode.ALWAYS, people.group_admin)
        message = notification(people.mm_mad, people.engineer)
        assert message.recipient_users == [people.mm_mad.pk]

    def test_the_persons_own_choice_wins_over_the_administrators_at_queueing(
        self, people: People, notification: Callable[..., EmailOutbox]
    ) -> None:
        """Q93: an own *on* overrides an administrator's *never* and an *off* for everyone; an
        own *off* overrides *always*; the email switch still wins over an own *on*."""
        admin, mm, eng, ga = people.group_admin, people.mm_mad, people.engineer, people.group_admin
        set_default(NOTICE, False, admin)
        set_mode(mm, NOTICE, Mode.NEVER, admin)
        choose(mm, NOTICE, True)
        set_mode(eng, NOTICE, Mode.ALWAYS, admin)
        choose(eng, NOTICE, False)
        set_mode(ga, NOTICE, Mode.ALWAYS, admin)
        choose(ga, NOTICE, True)
        refuse(ga)
        message = notification(mm, eng, ga)
        assert message.recipient_users == [mm.pk]

    def test_an_in_app_choice_changes_nothing_by_email(
        self, people: People, notification: Callable[..., EmailOutbox]
    ) -> None:
        choose(people.mm_mad, NOTICE, False, Channel.IN_APP)
        switch(people.engineer, Channel.IN_APP, False)
        message = notification(people.mm_mad, people.engineer)
        assert sorted(message.recipient_users) == sorted([people.mm_mad.pk, people.engineer.pk])

    def test_only_a_notification_may_name_a_notice(self, people: People) -> None:
        with pytest.raises(IntegrityError), transaction.atomic():
            EmailOutbox.objects.create(
                kind=Kind.SECURITY,
                sensitive=True,
                notice=NOTICE,
                recipient_address="x@example.org",
                eligibility_rule=eligibility.ACCOUNT_ACTIVE,
                eligibility_object_type="accounts.user",
                eligibility_object_id=people.mm_mad.pk,
                from_email="g <no-reply@localhost>",
                subject="s",
                body="b",
                expires_at="2030-01-01T00:00:00Z",
            )

    def test_security_mail_and_dispatches_carry_no_notice(
        self,
        security_mail: Callable[..., EmailOutbox],
        dispatch: Callable[..., EmailOutbox],
    ) -> None:
        assert security_mail().notice == ""
        assert dispatch().notice == ""

    def test_the_notice_is_frozen_at_insert(
        self, people: People, notification: Callable[..., EmailOutbox]
    ) -> None:
        message = notification(people.mm_mad)
        with pytest.raises(IntegrityError, match="frozen"), transaction.atomic():
            EmailOutbox.objects.filter(pk=message.pk).update(notice=Notice.REQUEST_APPROVED)
        assert EmailOutbox.objects.get(pk=message.pk).notice == NOTICE


class TestAtTheClaim:
    def test_a_change_after_queueing_is_followed(
        self, people: People, notification: Callable[..., EmailOutbox]
    ) -> None:
        admin, mm, ga = people.group_admin, people.mm_mad, people.group_admin
        message = notification(mm, ga)  # the group administrator is a recipient too
        assert eligibility.eligible_recipients(message) == sorted([mm, ga], key=lambda u: u.pk)
        set_default(NOTICE, False, admin)
        assert eligibility.eligible_recipients(message) == []
        set_mode(mm, NOTICE, Mode.ALWAYS, admin)
        assert eligibility.eligible_recipients(message) == [mm]
        refuse(mm)  # the person's refusal wins over always
        assert eligibility.eligible_recipients(message) == []

    def test_the_persons_own_choice_made_after_queueing_is_followed(
        self, people: People, notification: Callable[..., EmailOutbox], tmp_path: Path
    ) -> None:
        """Queued while both wanted it; then one turned it off themselves despite *always*, the
        other has *never* and turned it on themselves: the claim follows both choices."""
        admin, mm, eng = people.group_admin, people.mm_mad, people.engineer
        message = notification(mm, eng)
        set_mode(mm, NOTICE, Mode.ALWAYS, admin)
        choose(mm, NOTICE, False)
        set_mode(eng, NOTICE, Mode.NEVER, admin)
        set_default(NOTICE, False, admin)
        assert eligibility.eligible_recipients(message) == []
        choose(eng, NOTICE, True)
        assert eligibility.eligible_recipients(message) == [eng]
        with fake_smtp() as server, override_settings(MAILERS=smtp_mailers(server.port)):
            assert a_worker(tmp_path).tick() is True
        (received,) = server.received
        assert received.recipients == [eng.email]
        assert EmailOutbox.objects.get(pk=message.pk).status == Status.SENT

    def test_back_to_following_brings_the_administrators_setting_back(
        self, people: People, notification: Callable[..., EmailOutbox]
    ) -> None:
        admin, mm = people.group_admin, people.mm_mad
        message = notification(mm)
        set_mode(mm, NOTICE, Mode.NEVER, admin)
        choose(mm, NOTICE, True)
        assert eligibility.eligible_recipients(message) == [mm]
        NoticeChoice.objects.filter(user=mm).delete()
        assert eligibility.eligible_recipients(message) == []

    def test_a_row_without_a_notice_follows_the_refusal_only(
        self, org: Org, people: People
    ) -> None:
        """A message queued before M15 has a blank notice: no notice's settings apply."""
        message = EmailOutbox.objects.create(
            kind=Kind.NOTIFICATION,
            recipient_users=[people.mm_mad.pk, people.engineer.pk],
            eligibility_rule=Capability.PROJECT_VIEW,
            eligibility_object_type="projects.project",
            eligibility_object_id=org.hospital.pk,
            from_email="g <no-reply@localhost>",
            subject="s",
            body="b",
        )
        assert message.notice == ""
        for notice in Notice:
            set_default(notice, False, people.group_admin)
        refuse(people.engineer)
        assert eligibility.eligible_recipients(message) == [people.mm_mad]

    def test_the_worker_sends_nothing_to_who_no_longer_wants_it(
        self,
        people: People,
        notification: Callable[..., EmailOutbox],
        tmp_path: Path,
    ) -> None:
        """Turned off for everyone after queueing; kept for one person: only they get it."""
        message = notification(people.mm_mad, people.engineer)
        set_default(NOTICE, False, people.group_admin)
        set_mode(people.engineer, NOTICE, Mode.ALWAYS, people.group_admin)
        with fake_smtp() as server, override_settings(MAILERS=smtp_mailers(server.port)):
            assert a_worker(tmp_path).tick() is True
        (received,) = server.received
        assert received.recipients == [people.engineer.email]
        message.refresh_from_db()
        assert message.status == Status.SENT
        assert message.current_attempt is not None
        assert message.current_attempt.recipient_users == [people.engineer.pk]

    def test_turned_off_for_everyone_the_message_is_suppressed(
        self,
        people: People,
        notification: Callable[..., EmailOutbox],
        tmp_path: Path,
    ) -> None:
        message = notification(people.mm_mad)
        set_default(NOTICE, False, people.group_admin)
        with fake_smtp() as server, override_settings(MAILERS=smtp_mailers(server.port)):
            a_worker(tmp_path).tick()
        assert server.received == []
        assert EmailOutbox.objects.get(pk=message.pk).status == Status.SUPPRESSED
