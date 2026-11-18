"""The setting for everyone and a person's settings (M15.2, Q87, Q89).

``set_notice_default`` and ``set_person_notices`` are ``org.manage`` operations: allowed for a
group administrator with session MFA, refused to every other role, to a revoked or expired grant
and to a session that never passed MFA; a person out of reach is a 404 and a staff account is
refused. Every change is audited with before and after, nothing is written on a refusal or when
nothing differs, and a double tap replays.
"""

import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.access.exceptions import AccessDenied, MFARequired, NotInScope
from apps.access.roles import Role
from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.core.idempotency import IdempotencyConflict, issue_key
from apps.core.models import IdempotencyRecord
from apps.notifications import services
from apps.notifications.kinds import Channel, Mode, Notice
from apps.notifications.models import NoticeDefault, NoticeSetting
from tests.org import People
from tests.support import error_codes, grant, principal_for

pytestmark = pytest.mark.django_db

EVERYONE_OPERATION = services.NOTICE_DEFAULT_OPERATION
PERSON_OPERATION = services.PERSON_NOTICES_OPERATION

# Every actor of the matrix besides the group administrator: none holds ``org.manage``.
OTHER_ROLES = [
    "mm_mad",
    "engineer",
    "wo_mad",
    "tech_mad",
    "site_manager",
    "partner",
    "mixed",
    "nobody",
]


def written() -> tuple[int, int, int]:
    """What a refusal must leave alone: rows of both tables and audit events."""
    return NoticeDefault.objects.count(), NoticeSetting.objects.count(), AuditEvent.objects.count()


def events_of(action: str) -> list[AuditEvent]:
    return list(AuditEvent.objects.filter(action=action).order_by("occurred_at"))


def revoked_admin(make_user: Callable[..., User]) -> User:
    user = make_user("former-admin@example.org")
    membership = grant(user, Role.GROUP_ADMIN)
    membership.revoked_at = membership.valid_from
    membership.save(update_fields=["revoked_at"])
    return user


def expired_admin(make_user: Callable[..., User]) -> User:
    user = make_user("lapsed-admin@example.org")
    grant(
        user,
        Role.GROUP_ADMIN,
        valid_from=timezone.now() - timedelta(hours=2),
        valid_until=timezone.now() - timedelta(hours=1),
    )
    return user


class TestSetNoticeDefault:
    def test_a_group_administrator_turns_a_notice_off_and_on(self, people: People) -> None:
        principal = principal_for(people.group_admin)
        row = services.set_notice_default(principal, Notice.REQUEST_APPROVED, enabled=False)
        assert (row.notice, row.channel, row.enabled) == (
            Notice.REQUEST_APPROVED,
            Channel.EMAIL,
            False,
        )
        assert row.changed_by == people.group_admin
        first = events_of("notification.default_changed")[0]
        assert first.object_id == row.pk and first.branch_id is None
        assert first.actor == people.group_admin
        assert first.data == {
            "notice": "REQUEST_APPROVED",
            "channel": "EMAIL",
            "before": True,
            "after": False,
        }
        services.set_notice_default(principal, Notice.REQUEST_APPROVED, enabled=True)
        row.refresh_from_db()
        assert row.enabled is True
        assert events_of("notification.default_changed")[1].data["before"] is False
        assert NoticeDefault.objects.count() == 1

    def test_the_first_decision_saying_on_records_who_decided_but_audits_nothing(
        self, people: People
    ) -> None:
        # No row already meant on: nothing changed, so no audit event is made.
        services.set_notice_default(
            principal_for(people.group_admin), "REQUEST_EDITED", enabled=True
        )
        assert NoticeDefault.objects.get().enabled is True
        assert not events_of("notification.default_changed")

    def test_the_same_decision_again_writes_nothing(self, people: People) -> None:
        principal = principal_for(people.group_admin)
        services.set_notice_default(principal, Notice.REQUEST_REJECTED, enabled=False)
        before = written()
        changed_at = NoticeDefault.objects.get().changed_at
        services.set_notice_default(principal, Notice.REQUEST_REJECTED, enabled=False)
        assert written() == before
        assert NoticeDefault.objects.get().changed_at == changed_at

    def test_each_notice_has_its_own_row(self, people: People) -> None:
        principal = principal_for(people.group_admin)
        for notice in Notice:
            services.set_notice_default(principal, notice, enabled=False)
        assert NoticeDefault.objects.filter(enabled=False).count() == len(Notice)

    def test_people_with_their_own_setting_keep_it(self, people: People) -> None:
        setting = NoticeSetting.objects.create(
            user=people.tech_mad,
            notice=Notice.REQUEST_APPROVED,
            channel=Channel.EMAIL,
            mode=Mode.ALWAYS,
            set_by=people.group_admin,
        )
        services.set_notice_default(
            principal_for(people.group_admin), Notice.REQUEST_APPROVED, enabled=False
        )
        setting.refresh_from_db()
        assert setting.mode == Mode.ALWAYS

    @pytest.mark.parametrize("actor", OTHER_ROLES)
    def test_only_org_manage(self, people: People, actor: str) -> None:
        before = written()
        with pytest.raises(AccessDenied):
            services.set_notice_default(
                principal_for(getattr(people, actor)), Notice.REQUEST_APPROVED, enabled=False
            )
        assert written() == before

    @pytest.mark.parametrize("actor", ["operator", "deactivated", "expired", "revoked"])
    def test_a_superuser_and_an_inactive_grant_hold_no_business_capability(
        self, people: People, actor: str
    ) -> None:
        before = written()
        with pytest.raises(AccessDenied):
            services.set_notice_default(
                principal_for(getattr(people, actor)), Notice.REQUEST_APPROVED, enabled=False
            )
        assert written() == before

    def test_a_revoked_or_lapsed_administrator_may_not(
        self, make_user: Callable[..., User]
    ) -> None:
        for user in (revoked_admin(make_user), expired_admin(make_user)):
            with pytest.raises(AccessDenied):
                services.set_notice_default(
                    principal_for(user), Notice.REQUEST_APPROVED, enabled=False
                )
        assert written() == (0, 0, written()[2])
        assert not events_of("notification.default_changed")

    def test_a_session_without_mfa_is_refused(self, people: People) -> None:
        with pytest.raises(MFARequired):
            services.set_notice_default(
                principal_for(people.group_admin, mfa_at=None),
                Notice.REQUEST_APPROVED,
                enabled=False,
            )
        assert not NoticeDefault.objects.exists()

    @pytest.mark.parametrize(
        ("notice", "channel", "field"),
        [("NOT_A_NOTICE", "EMAIL", "notice"), ("REQUEST_APPROVED", "TELEGRAM", "channel")],
    )
    def test_unknown_choices_are_refused(
        self, people: People, notice: str, channel: str, field: str
    ) -> None:
        with pytest.raises(ValidationError) as exc:
            services.set_notice_default(
                principal_for(people.group_admin), notice, enabled=False, channel=channel
            )
        assert exc.value.message_dict.keys() == {field}
        assert error_codes(exc.value) == {"invalid_choice"}
        assert not NoticeDefault.objects.exists()


class TestSetNoticeDefaultIdempotency:
    def call(self, actor: User, key: str, *, enabled: bool = False) -> NoticeDefault:
        return services.set_notice_default(
            principal_for(actor),
            Notice.REQUEST_TO_REVIEW,
            enabled=enabled,
            idempotency_key=key,
        )

    def test_a_double_tap_replays_the_same_row(self, people: People) -> None:
        key = issue_key(people.group_admin.pk, EVERYONE_OPERATION)
        first = self.call(people.group_admin, key)
        second = self.call(people.group_admin, key)
        assert first.pk == second.pk
        assert len(events_of("notification.default_changed")) == 1
        assert IdempotencyRecord.objects.filter(operation=EVERYONE_OPERATION).count() == 1

    def test_the_same_key_with_another_payload_is_a_conflict(self, people: People) -> None:
        key = issue_key(people.group_admin.pk, EVERYONE_OPERATION)
        self.call(people.group_admin, key, enabled=False)
        with pytest.raises(IdempotencyConflict):
            self.call(people.group_admin, key, enabled=True)
        assert NoticeDefault.objects.get().enabled is False

    def test_a_replay_authorizes_again(
        self, people: People, make_user: Callable[..., User]
    ) -> None:
        admin = make_user("second-admin@example.org")
        membership = grant(admin, Role.GROUP_ADMIN)
        key = issue_key(admin.pk, EVERYONE_OPERATION)
        self.call(admin, key)
        membership.revoked_at = membership.valid_from
        membership.save(update_fields=["revoked_at"])
        with pytest.raises(AccessDenied):
            self.call(admin, key)
        assert len(events_of("notification.default_changed")) == 1


class TestSetPersonNotices:
    def set(self, actor: User, person: User, modes: dict[Any, Any], **extra: Any) -> User:
        return services.set_person_notices(principal_for(actor), person.pk, modes, **extra)

    def test_a_group_administrator_sets_and_clears_a_person_s_modes(self, people: People) -> None:
        person, admin = people.mm_mad, people.group_admin
        self.set(
            admin, person, {Notice.REQUEST_TO_REVIEW: Mode.NEVER, "REQUEST_ASSIGNED": "ALWAYS"}
        )
        rows = {row.notice: row for row in NoticeSetting.objects.filter(user=person)}
        assert {key: row.mode for key, row in rows.items()} == {
            "REQUEST_TO_REVIEW": "NEVER",
            "REQUEST_ASSIGNED": "ALWAYS",
        }
        assert all(row.set_by == admin and row.channel == Channel.EMAIL for row in rows.values())
        event = events_of("notification.person_changed")[0]
        assert event.object_id == person.pk and event.branch_id is None
        assert event.actor == admin
        assert event.data == {
            "channel": "EMAIL",
            "changes": {
                "REQUEST_TO_REVIEW": {"before": None, "after": "NEVER"},
                "REQUEST_ASSIGNED": {"before": None, "after": "ALWAYS"},
            },
        }
        self.set(
            admin, person, {Notice.REQUEST_TO_REVIEW: Mode.ALWAYS, Notice.REQUEST_ASSIGNED: None}
        )
        assert dict(NoticeSetting.objects.filter(user=person).values_list("notice", "mode")) == {
            "REQUEST_TO_REVIEW": "ALWAYS"
        }
        assert events_of("notification.person_changed")[1].data["changes"] == {
            "REQUEST_TO_REVIEW": {"before": "NEVER", "after": "ALWAYS"},
            "REQUEST_ASSIGNED": {"before": "ALWAYS", "after": None},
        }

    def test_only_the_notices_named_change(self, people: People) -> None:
        self.set(people.group_admin, people.tech_mad, {Notice.REQUEST_APPROVED: Mode.NEVER})
        self.set(people.group_admin, people.tech_mad, {Notice.REQUEST_REJECTED: Mode.NEVER})
        assert NoticeSetting.objects.filter(user=people.tech_mad).count() == 2

    def test_another_person_is_left_alone(self, people: People) -> None:
        self.set(people.group_admin, people.tech_mad, {Notice.REQUEST_APPROVED: Mode.NEVER})
        assert not NoticeSetting.objects.filter(user=people.mm_mad).exists()

    def test_nothing_that_differs_writes_nothing(self, people: People) -> None:
        self.set(people.group_admin, people.tech_mad, {Notice.REQUEST_APPROVED: Mode.NEVER})
        before = written()
        self.set(
            people.group_admin,
            people.tech_mad,
            {Notice.REQUEST_APPROVED: Mode.NEVER, Notice.REQUEST_EDITED: None},
        )
        self.set(people.group_admin, people.tech_mad, {})
        assert written() == before

    def test_an_administrator_may_set_their_own(self, people: People) -> None:
        self.set(people.group_admin, people.group_admin, {Notice.REQUEST_TO_REVIEW: Mode.NEVER})
        assert NoticeSetting.objects.get(user=people.group_admin).mode == Mode.NEVER

    def test_the_person_is_told_nothing(self, people: People) -> None:
        from apps.notifications.models import EmailOutbox

        self.set(people.group_admin, people.tech_mad, {Notice.REQUEST_APPROVED: Mode.NEVER})
        assert not EmailOutbox.objects.exists()

    @pytest.mark.parametrize("actor", OTHER_ROLES)
    def test_only_org_manage(self, people: People, actor: str) -> None:
        before = written()
        with pytest.raises(AccessDenied):
            self.set(getattr(people, actor), people.tech_mad, {Notice.REQUEST_APPROVED: Mode.NEVER})
        assert written() == before

    @pytest.mark.parametrize("actor", ["operator", "deactivated", "expired", "revoked"])
    def test_a_superuser_and_an_inactive_grant_are_refused(
        self, people: People, actor: str
    ) -> None:
        before = written()
        with pytest.raises(AccessDenied):
            self.set(getattr(people, actor), people.tech_mad, {Notice.REQUEST_APPROVED: Mode.NEVER})
        assert written() == before

    def test_a_revoked_or_lapsed_administrator_may_not(
        self, people: People, make_user: Callable[..., User]
    ) -> None:
        for user in (revoked_admin(make_user), expired_admin(make_user)):
            with pytest.raises(AccessDenied):
                self.set(user, people.tech_mad, {Notice.REQUEST_APPROVED: Mode.NEVER})
        assert not NoticeSetting.objects.exists()

    def test_a_session_without_mfa_is_refused(self, people: People) -> None:
        with pytest.raises(MFARequired):
            services.set_person_notices(
                principal_for(people.group_admin, mfa_at=None),
                people.tech_mad.pk,
                {Notice.REQUEST_APPROVED: Mode.NEVER},
            )
        assert not NoticeSetting.objects.exists()

    def test_a_person_who_does_not_exist_is_out_of_scope(self, people: People) -> None:
        with pytest.raises(NotInScope):
            services.set_person_notices(
                principal_for(people.group_admin),
                uuid.uuid7(),
                {Notice.REQUEST_APPROVED: Mode.NEVER},
            )
        assert not NoticeSetting.objects.exists()

    def test_a_staff_account_is_refused(self, people: People) -> None:
        with pytest.raises(AccessDenied):
            self.set(people.group_admin, people.operator, {Notice.REQUEST_APPROVED: Mode.NEVER})
        assert not NoticeSetting.objects.exists()

    def test_a_person_who_left_is_refused(self, people: People) -> None:
        with pytest.raises(ValidationError) as refused:
            self.set(people.group_admin, people.deactivated, {Notice.REQUEST_APPROVED: Mode.NEVER})
        assert error_codes(refused.value) == {"left"}
        assert not NoticeSetting.objects.exists()
        assert not events_of("notification.person_changed")

    def test_a_superuser_may_not_either_without_org_manage(self, people: People) -> None:
        with pytest.raises(AccessDenied):
            self.set(people.operator, people.tech_mad, {Notice.REQUEST_APPROVED: Mode.NEVER})

    @pytest.mark.parametrize(
        "modes",
        [
            {"NOT_A_NOTICE": "NEVER"},
            {"REQUEST_APPROVED": "SOMETIMES"},
        ],
    )
    def test_unknown_choices_are_refused(self, people: People, modes: dict[str, str]) -> None:
        with pytest.raises(ValidationError) as exc:
            self.set(people.group_admin, people.tech_mad, modes)
        assert error_codes(exc.value) == {"invalid_choice"}
        assert not NoticeSetting.objects.exists()

    def test_an_unknown_channel_is_refused(self, people: People) -> None:
        with pytest.raises(ValidationError):
            self.set(
                people.group_admin,
                people.tech_mad,
                {Notice.REQUEST_APPROVED: Mode.NEVER},
                channel="TELEGRAM",
            )
        assert not NoticeSetting.objects.exists()


class TestSetPersonNoticesIdempotency:
    def call(
        self, actor: User, key: str, mode: Mode = Mode.NEVER, person: User | None = None
    ) -> User:
        return services.set_person_notices(
            principal_for(actor),
            (person or actor).pk,
            {Notice.REQUEST_APPROVED: mode},
            idempotency_key=key,
        )

    def test_a_double_tap_replays(self, people: People) -> None:
        key = issue_key(people.group_admin.pk, PERSON_OPERATION)
        first = self.call(people.group_admin, key, person=people.tech_mad)
        second = self.call(people.group_admin, key, person=people.tech_mad)
        assert first.pk == second.pk == people.tech_mad.pk
        assert len(events_of("notification.person_changed")) == 1

    def test_the_same_key_with_another_payload_is_a_conflict(self, people: People) -> None:
        key = issue_key(people.group_admin.pk, PERSON_OPERATION)
        self.call(people.group_admin, key, person=people.tech_mad)
        with pytest.raises(IdempotencyConflict):
            self.call(people.group_admin, key, mode=Mode.ALWAYS, person=people.tech_mad)
        assert NoticeSetting.objects.get().mode == Mode.NEVER

    def test_a_replay_authorizes_again(
        self, people: People, make_user: Callable[..., User]
    ) -> None:
        admin = make_user("third-admin@example.org")
        membership = grant(admin, Role.GROUP_ADMIN)
        key = issue_key(admin.pk, PERSON_OPERATION)
        self.call(admin, key, person=people.tech_mad)
        membership.revoked_at = membership.valid_from
        membership.save(update_fields=["revoked_at"])
        with pytest.raises(AccessDenied):
            self.call(admin, key, person=people.tech_mad)
        assert len(events_of("notification.person_changed")) == 1
