"""A person's own channels and choices (M17.2, Q93).

``set_own_channels`` and ``set_own_notices`` act on the principal's own rows only and take no user
id. Any current member may use them (privileged roles with session MFA); a revoked, expired,
deactivated or offboarded account is refused; every change is audited with before and after,
and a call that changes nothing writes and audits nothing.
"""

import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.access.exceptions import AccessDenied, MFARequired
from apps.access.roles import Role
from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.core.idempotency import IdempotencyConflict, IdempotencyKeyInvalid, issue_key
from apps.notifications import resolution, services
from apps.notifications.kinds import Channel, Mode, Notice
from apps.notifications.models import (
    ChannelSwitch,
    NoticeChoice,
    NoticeSetting,
    NotificationPreference,
)
from tests.org import People
from tests.support import error_codes, grant, principal_for

pytestmark = pytest.mark.django_db

ACTION = "notification.own_changed"
APPROVED = Notice.REQUEST_APPROVED
EMAIL, IN_APP = Channel.EMAIL, Channel.IN_APP

# Every role of the matrix, privileged or not: each may change its own.
EVERY_ROLE = [
    "group_admin",
    "mm_mad",
    "engineer",
    "wo_mad",
    "tech_mad",
    "site_manager",
    "partner",
    "mixed",
]
# Roles that hold the capability only through a privileged membership.
PRIVILEGED = ["group_admin", "mm_mad", "engineer"]
REFUSED = ["nobody", "deactivated", "expired", "revoked", "operator"]


def written() -> tuple[int, int, int, int]:
    """What a refusal must leave alone: the own rows of every kind and the audit events."""
    return (
        NotificationPreference.objects.count(),
        ChannelSwitch.objects.count(),
        NoticeChoice.objects.count(),
        AuditEvent.objects.count(),
    )


def events() -> list[AuditEvent]:
    return list(AuditEvent.objects.filter(action=ACTION).order_by("occurred_at"))


def offboarded(user: User) -> User:
    user.offboarded_at = timezone.now()
    user.save(update_fields=["offboarded_at"])
    return user


class TestSetOwnChannels:
    def test_a_person_turns_a_channel_off_and_on(self, people: People) -> None:
        principal = principal_for(people.tech_mad)
        services.set_own_channels(principal, {"IN_APP": False})
        switch = ChannelSwitch.objects.get(user=people.tech_mad)
        assert (switch.channel, switch.enabled) == (IN_APP, False)
        assert people.tech_mad.pk not in resolution.wanted([people.tech_mad.pk], APPROVED, IN_APP)
        services.set_own_channels(principal, {IN_APP: True})
        switch.refresh_from_db()
        assert switch.enabled is True
        assert people.tech_mad.pk in resolution.wanted([people.tech_mad.pk], APPROVED, IN_APP)
        first, second = events()
        assert first.data == {"channels": {"IN_APP": {"before": True, "after": False}}}
        assert second.data == {"channels": {"IN_APP": {"before": False, "after": True}}}
        assert first.object_id == people.tech_mad.pk and first.branch_id is None
        assert first.actor == people.tech_mad

    def test_email_is_the_existing_switch(self, people: People) -> None:
        principal = principal_for(people.tech_mad)
        services.set_own_channels(principal, {EMAIL: False})
        assert NotificationPreference.objects.get(user=people.tech_mad).email_opt_out is True
        assert not ChannelSwitch.objects.exists()
        services.set_own_channels(principal, {EMAIL: True})
        assert NotificationPreference.objects.get(user=people.tech_mad).email_opt_out is False
        assert [e.data["channels"]["EMAIL"] for e in events()] == [
            {"before": True, "after": False},
            {"before": False, "after": True},
        ]

    def test_several_channels_are_one_event(self, people: People) -> None:
        services.set_own_channels(principal_for(people.tech_mad), {EMAIL: False, IN_APP: False})
        (event,) = events()
        assert set(event.data["channels"]) == {"EMAIL", "IN_APP"}

    def test_only_the_channels_named_change(self, people: People) -> None:
        services.set_own_channels(principal_for(people.tech_mad), {IN_APP: False})
        assert not NotificationPreference.objects.exists()
        assert people.tech_mad.pk in resolution.wanted([people.tech_mad.pk], APPROVED, EMAIL)

    def test_a_change_that_alters_nothing_writes_nothing(self, people: People) -> None:
        principal = principal_for(people.tech_mad)
        services.set_own_channels(principal, {EMAIL: True, IN_APP: True})  # the defaults
        assert written() == (0, 0, 0, 0)
        services.set_own_channels(principal, {IN_APP: False})
        before = written()
        changed_at = ChannelSwitch.objects.get().changed_at
        services.set_own_channels(principal, {IN_APP: False, EMAIL: True})
        services.set_own_channels(principal, {})
        assert written() == before
        assert ChannelSwitch.objects.get().changed_at == changed_at

    def test_only_the_principals_own_rows_change(self, people: People) -> None:
        services.set_own_channels(principal_for(people.tech_mad), {EMAIL: False, IN_APP: False})
        assert not NotificationPreference.objects.filter(user=people.mm_mad).exists()
        assert not ChannelSwitch.objects.filter(user=people.mm_mad).exists()
        assert people.mm_mad.pk in resolution.wanted([people.mm_mad.pk], APPROVED, IN_APP)

    def test_the_own_choices_are_kept_while_a_channel_is_off(self, people: People) -> None:
        principal = principal_for(people.tech_mad)
        services.set_own_notices(principal, {(APPROVED, IN_APP): True})
        services.set_own_channels(principal, {IN_APP: False})
        assert NoticeChoice.objects.filter(user=people.tech_mad).count() == 1
        assert people.tech_mad.pk not in resolution.wanted([people.tech_mad.pk], APPROVED, IN_APP)
        services.set_own_channels(principal, {IN_APP: True})
        assert people.tech_mad.pk in resolution.wanted([people.tech_mad.pk], APPROVED, IN_APP)

    def test_nothing_is_sent_to_the_person_or_anybody(self, people: People) -> None:
        from apps.notifications.models import EmailOutbox

        services.set_own_channels(principal_for(people.tech_mad), {EMAIL: False})
        assert not EmailOutbox.objects.exists()

    @pytest.mark.parametrize("actor", EVERY_ROLE)
    def test_every_role_changes_its_own(self, people: People, actor: str) -> None:
        user = getattr(people, actor)
        returned = services.set_own_channels(principal_for(user), {IN_APP: False})
        assert returned.pk == user.pk
        assert ChannelSwitch.objects.get().user == user

    @pytest.mark.parametrize("actor", REFUSED)
    def test_without_a_current_membership_it_is_refused(self, people: People, actor: str) -> None:
        with pytest.raises(AccessDenied):
            services.set_own_channels(principal_for(getattr(people, actor)), {IN_APP: False})
        assert written() == (0, 0, 0, 0)

    def test_a_revoked_or_lapsed_grant_is_refused(
        self, people: People, make_user: Callable[..., User]
    ) -> None:
        user = make_user("lapsed@example.org")
        grant(
            user,
            Role.GROUP_ADMIN,
            valid_from=timezone.now() - timedelta(hours=2),
            valid_until=timezone.now() - timedelta(hours=1),
        )
        with pytest.raises(AccessDenied):
            services.set_own_channels(principal_for(user), {IN_APP: False})
        assert written() == (0, 0, 0, 0)

    def test_an_offboarded_account_is_refused(self, people: People) -> None:
        user = offboarded(people.tech_mad)
        with pytest.raises(AccessDenied):
            services.set_own_channels(principal_for(user), {IN_APP: False})
        assert written() == (0, 0, 0, 0)

    @pytest.mark.parametrize("actor", PRIVILEGED)
    def test_a_privileged_role_needs_session_mfa(self, people: People, actor: str) -> None:
        with pytest.raises(MFARequired):
            services.set_own_channels(
                principal_for(getattr(people, actor), mfa_at=None), {IN_APP: False}
            )
        assert written() == (0, 0, 0, 0)

    def test_another_role_needs_no_mfa(self, people: People) -> None:
        services.set_own_channels(principal_for(people.tech_mad, mfa_at=None), {IN_APP: False})
        assert ChannelSwitch.objects.count() == 1

    @pytest.mark.parametrize(
        "channels",
        [{"TELEGRAM": False}, {"in_app": False}, {IN_APP: "off"}, {IN_APP: None}, {IN_APP: 0}],
    )
    def test_unknown_choices_are_refused(self, people: People, channels: dict[Any, Any]) -> None:
        with pytest.raises(ValidationError) as refused:
            services.set_own_channels(principal_for(people.tech_mad), channels)
        assert error_codes(refused.value) == {"invalid_choice"}
        assert written() == (0, 0, 0, 0)


class TestSetOwnNotices:
    def test_a_person_chooses_and_goes_back_to_follow(self, people: People) -> None:
        principal = principal_for(people.tech_mad)
        services.set_own_notices(
            principal, {(APPROVED, "EMAIL"): False, ("REQUEST_EDITED", IN_APP): True}
        )
        rows = {(r.notice, r.channel): r.enabled for r in NoticeChoice.objects.all()}
        assert rows == {("REQUEST_APPROVED", "EMAIL"): False, ("REQUEST_EDITED", "IN_APP"): True}
        services.set_own_notices(
            principal, {(APPROVED, EMAIL): None, (Notice.REQUEST_EDITED, IN_APP): False}
        )
        rows = {(r.notice, r.channel): r.enabled for r in NoticeChoice.objects.all()}
        assert rows == {("REQUEST_EDITED", "IN_APP"): False}
        first, second = events()
        assert first.data == {
            "notices": {
                "EMAIL": {"REQUEST_APPROVED": {"before": None, "after": False}},
                "IN_APP": {"REQUEST_EDITED": {"before": None, "after": True}},
            }
        }
        assert second.data == {
            "notices": {
                "EMAIL": {"REQUEST_APPROVED": {"before": False, "after": None}},
                "IN_APP": {"REQUEST_EDITED": {"before": True, "after": False}},
            }
        }
        assert first.object_id == people.tech_mad.pk and first.branch_id is None

    def test_a_choice_wins_over_the_administrators_in_both_directions(self, people: People) -> None:
        tech = people.tech_mad
        NoticeSetting.objects.create(
            user=tech, notice=APPROVED, channel=EMAIL, mode=Mode.NEVER, set_by=people.group_admin
        )
        NoticeSetting.objects.create(
            user=tech,
            notice="REQUEST_EDITED",
            channel=EMAIL,
            mode=Mode.ALWAYS,
            set_by=people.group_admin,
        )
        assert tech.pk not in resolution.wanted([tech.pk], APPROVED, EMAIL)
        services.set_own_notices(
            principal_for(tech), {(APPROVED, EMAIL): True, ("REQUEST_EDITED", EMAIL): False}
        )
        assert tech.pk in resolution.wanted([tech.pk], APPROVED, EMAIL)
        assert tech.pk not in resolution.wanted([tech.pk], "REQUEST_EDITED", EMAIL)
        # Back to follow: the administrators' settings apply again, untouched.
        services.set_own_notices(
            principal_for(tech), {(APPROVED, EMAIL): None, ("REQUEST_EDITED", EMAIL): None}
        )
        assert tech.pk not in resolution.wanted([tech.pk], APPROVED, EMAIL)
        assert tech.pk in resolution.wanted([tech.pk], "REQUEST_EDITED", EMAIL)
        assert NoticeSetting.objects.filter(user=tech).count() == 2

    def test_a_choice_never_wins_over_the_persons_switch(self, people: People) -> None:
        principal = principal_for(people.tech_mad)
        services.set_own_channels(principal, {EMAIL: False})
        services.set_own_notices(principal, {(APPROVED, EMAIL): True})
        assert people.tech_mad.pk not in resolution.wanted([people.tech_mad.pk], APPROVED, EMAIL)

    def test_the_other_persons_rows_are_left_alone(self, people: People) -> None:
        services.set_own_notices(principal_for(people.tech_mad), {(APPROVED, EMAIL): False})
        assert not NoticeChoice.objects.filter(user=people.mm_mad).exists()
        assert people.mm_mad.pk in resolution.wanted([people.mm_mad.pk], APPROVED, EMAIL)

    def test_a_choice_that_alters_nothing_writes_nothing(self, people: People) -> None:
        principal = principal_for(people.tech_mad)
        services.set_own_notices(principal, {(APPROVED, EMAIL): None})  # follow, already
        assert written() == (0, 0, 0, 0)
        services.set_own_notices(principal, {(APPROVED, IN_APP): False})
        before = written()
        changed_at = NoticeChoice.objects.get().changed_at
        services.set_own_notices(
            principal, {(APPROVED, IN_APP): False, ("REQUEST_EDITED", EMAIL): None}
        )
        services.set_own_notices(principal, {})
        assert written() == before
        assert NoticeChoice.objects.get().changed_at == changed_at

    @pytest.mark.parametrize("actor", EVERY_ROLE)
    def test_every_role_chooses_for_itself(self, people: People, actor: str) -> None:
        user = getattr(people, actor)
        services.set_own_notices(principal_for(user), {(APPROVED, IN_APP): False})
        assert NoticeChoice.objects.get().user == user

    @pytest.mark.parametrize("actor", REFUSED)
    def test_without_a_current_membership_it_is_refused(self, people: People, actor: str) -> None:
        with pytest.raises(AccessDenied):
            services.set_own_notices(
                principal_for(getattr(people, actor)), {(APPROVED, EMAIL): False}
            )
        assert written() == (0, 0, 0, 0)

    def test_an_offboarded_account_is_refused(self, people: People) -> None:
        user = offboarded(people.tech_mad)
        with pytest.raises(AccessDenied):
            services.set_own_notices(principal_for(user), {(APPROVED, EMAIL): False})
        assert written() == (0, 0, 0, 0)

    @pytest.mark.parametrize("actor", PRIVILEGED)
    def test_a_privileged_role_needs_session_mfa(self, people: People, actor: str) -> None:
        with pytest.raises(MFARequired):
            services.set_own_notices(
                principal_for(getattr(people, actor), mfa_at=None), {(APPROVED, EMAIL): False}
            )
        assert written() == (0, 0, 0, 0)

    @pytest.mark.parametrize(
        "choices",
        [
            {("NOT_A_NOTICE", "EMAIL"): False},
            {("REQUEST_APPROVED", "TELEGRAM"): False},
            {("REQUEST_APPROVED", "EMAIL"): "on"},
            {("REQUEST_APPROVED", "EMAIL"): 1},
            {"REQUEST_APPROVED": False},
            {("REQUEST_APPROVED",): False},
        ],
    )
    def test_unknown_choices_are_refused(self, people: People, choices: dict[Any, Any]) -> None:
        with pytest.raises(ValidationError) as refused:
            services.set_own_notices(principal_for(people.tech_mad), choices)
        assert error_codes(refused.value) == {"invalid_choice"}
        assert written() == (0, 0, 0, 0)

    def test_a_refused_batch_writes_none_of_it(self, people: People) -> None:
        with pytest.raises(ValidationError):
            services.set_own_notices(
                principal_for(people.tech_mad),
                {(APPROVED, EMAIL): False, ("NOT_A_NOTICE", EMAIL): False},
            )
        assert not NoticeChoice.objects.exists()


class TestIdempotency:
    def own_channels(self, user: User, key: str, on: bool = False) -> User:
        return services.set_own_channels(principal_for(user), {IN_APP: on}, idempotency_key=key)

    def own_notices(self, user: User, key: str, on: bool | None = False) -> User:
        return services.set_own_notices(
            principal_for(user), {(APPROVED, IN_APP): on}, idempotency_key=key
        )

    def test_a_double_tap_of_the_channels_replays(self, people: People) -> None:
        key = issue_key(people.tech_mad.pk, services.OWN_CHANNELS_OPERATION)
        first = self.own_channels(people.tech_mad, key)
        second = self.own_channels(people.tech_mad, key)
        assert first.pk == second.pk == people.tech_mad.pk
        assert len(events()) == 1

    def test_the_same_key_with_other_channels_is_a_conflict(self, people: People) -> None:
        key = issue_key(people.tech_mad.pk, services.OWN_CHANNELS_OPERATION)
        self.own_channels(people.tech_mad, key)
        with pytest.raises(IdempotencyConflict):
            self.own_channels(people.tech_mad, key, on=True)
        assert ChannelSwitch.objects.get().enabled is False

    def test_a_double_tap_of_the_choices_replays(self, people: People) -> None:
        key = issue_key(people.tech_mad.pk, services.OWN_NOTICES_OPERATION)
        self.own_notices(people.tech_mad, key)
        self.own_notices(people.tech_mad, key)
        assert len(events()) == 1
        assert NoticeChoice.objects.count() == 1

    def test_the_same_key_with_other_choices_is_a_conflict(self, people: People) -> None:
        key = issue_key(people.tech_mad.pk, services.OWN_NOTICES_OPERATION)
        self.own_notices(people.tech_mad, key)
        with pytest.raises(IdempotencyConflict):
            self.own_notices(people.tech_mad, key, on=None)
        assert NoticeChoice.objects.get().enabled is False

    def test_a_key_is_the_actors_own(self, people: People) -> None:
        key = issue_key(people.tech_mad.pk, services.OWN_NOTICES_OPERATION)
        # Another person cannot use a key issued to this one.
        with pytest.raises(IdempotencyKeyInvalid):
            self.own_notices(people.mm_mad, key)
        assert not NoticeChoice.objects.exists()

    def test_a_replay_authorizes_again(
        self, people: People, make_user: Callable[..., User]
    ) -> None:
        user = make_user("leaving@example.org")
        membership = grant(user, Role.GROUP_ADMIN)
        key = issue_key(user.pk, services.OWN_CHANNELS_OPERATION)
        self.own_channels(user, key)
        membership.revoked_at = membership.valid_from
        membership.save(update_fields=["revoked_at"])
        with pytest.raises(AccessDenied):
            self.own_channels(user, key)
        assert len(events()) == 1

    def test_a_replay_of_the_choices_authorizes_again(
        self, people: People, make_user: Callable[..., User]
    ) -> None:
        user = make_user("leaving-too@example.org")
        membership = grant(user, Role.GROUP_ADMIN)
        key = issue_key(user.pk, services.OWN_NOTICES_OPERATION)
        self.own_notices(user, key)
        membership.revoked_at = membership.valid_from
        membership.save(update_fields=["revoked_at"])
        with pytest.raises(AccessDenied):
            self.own_notices(user, key)


def test_the_signatures_name_no_other_person() -> None:
    import inspect

    for service in (services.set_own_channels, services.set_own_notices):
        names = set(inspect.signature(service).parameters)
        assert not names & {"user", "user_id", "person", "target"}, names


def test_a_random_id_is_not_a_parameter(people: People) -> None:
    with pytest.raises(TypeError):
        services.set_own_channels(  # type: ignore[call-arg]
            principal_for(people.tech_mad), {IN_APP: False}, user_id=uuid.uuid7()
        )
