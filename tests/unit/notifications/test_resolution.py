"""Business notices (M15.1): the registry, who wants a notice, and the outbox that follows it.

The three levels of Q87: the person's refusal of business email wins, then their own
setting, then the setting for everyone (no row: on). Applied when a message is queued and
again at the claim.
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
from apps.notifications.kinds import NOTICES, Channel, Mode, Notice, NoticeSpec, _registry
from apps.notifications.models import (
    EmailOutbox,
    Kind,
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


def set_default(notice: Notice, enabled: bool, by: User) -> None:
    NoticeDefault.objects.update_or_create(
        notice=notice, channel=Channel.EMAIL, defaults={"enabled": enabled, "changed_by": by}
    )


def set_mode(user: User, notice: Notice, mode: Mode, by: User) -> None:
    NoticeSetting.objects.update_or_create(
        user=user, notice=notice, channel=Channel.EMAIL, defaults={"mode": mode, "set_by": by}
    )


def refuse(user: User) -> None:
    NotificationPreference.objects.update_or_create(user=user, defaults={"email_opt_out": True})


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
        assert list(Channel) == [Channel.EMAIL]
        assert set(Mode) == {Mode.ALWAYS, Mode.NEVER}

    def test_a_spec_is_frozen(self) -> None:
        spec = NOTICES[NOTICE]
        with pytest.raises(AttributeError):
            spec.order = 1  # type: ignore[misc]
        assert isinstance(spec, NoticeSpec)


# opt-out, the person's setting, the setting for everyone (None: no row) → wanted
TABLE = [
    (
        opt_out,
        mode,
        default,
        (not opt_out) and (mode == Mode.ALWAYS or (mode is None and default is not False)),
    )
    for opt_out in (False, True)
    for mode in (None, Mode.ALWAYS, Mode.NEVER)
    for default in (None, True, False)
]


class TestResolution:
    @pytest.mark.parametrize(
        ("opt_out", "mode", "default", "expected"),
        TABLE,
        ids=[f"optout={o}-mode={m}-default={d}" for o, m, d, _ in TABLE],
    )
    def test_the_three_levels(
        self,
        people: People,
        opt_out: bool,
        mode: Mode | None,
        default: bool | None,
        expected: bool,
    ) -> None:
        person, admin = people.mm_mad, people.group_admin
        if opt_out:
            refuse(person)
        if mode is not None:
            set_mode(person, NOTICE, mode, admin)
        if default is not None:
            set_default(NOTICE, default, admin)
        assert (person.pk in resolution.wanted([person.pk], NOTICE)) is expected

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

    def test_without_a_notice_only_the_refusal_counts(self, people: People) -> None:
        """A message queued before M15: the settings of no notice apply to it."""
        admin, person, other = people.group_admin, people.mm_mad, people.engineer
        for notice in Notice:
            set_default(notice, False, admin)
            set_mode(person, notice, Mode.NEVER, admin)
        refuse(other)
        assert resolution.wanted([person.pk, other.pk], "") == {person.pk}

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
