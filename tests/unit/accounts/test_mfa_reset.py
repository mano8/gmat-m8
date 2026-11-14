"""An administrator resets the second factors of an account that lost them (M8.3), from the
command line or, since M10.4, from the person's page; and corrects a person's name (M10.4)."""

import logging
from collections.abc import Callable
from io import StringIO
from typing import Any

import pytest
from allauth.mfa.models import Authenticator
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import translation

from apps.access.exceptions import AccessDenied
from apps.access.roles import Role
from apps.accounts import services
from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.core.idempotency import IdempotencyConflict, issue_key
from apps.notifications.models import EmailOutbox
from apps.recovery.replay import HANDLERS
from tests.org import People
from tests.support import enroll_totp, error_codes, grant, principal_for

pytestmark = pytest.mark.django_db


@pytest.fixture
def lost(make_user: Callable[..., User]) -> User:
    """An account with a TOTP and a passkey, both lost with the phone."""
    user = make_user("tech@example.org")
    enroll_totp(user)
    Authenticator.objects.create(user=user, type=Authenticator.Type.WEBAUTHN, data={})
    return user


def reset(actor: User, target: User, **kwargs: Any) -> User:
    terms = {"reason": "Lost phone", "identity_checked": True, **kwargs}
    return services.reset_user_mfa(principal_for(actor), target.pk, **terms)


class TestService:
    def test_a_group_administrator_removes_every_factor_and_ends_every_session(
        self, people: People, lost: User
    ) -> None:
        epoch = lost.session_epoch
        reset(people.group_admin, lost, reason=" Lost phone ")
        lost.refresh_from_db()
        assert not Authenticator.objects.filter(user=lost).exists()
        assert lost.session_epoch > epoch
        event = AuditEvent.objects.get(action="user.mfa_reset")
        assert event.object_id == lost.pk and event.actor == people.group_admin
        assert event.data == {
            "factors_removed": ["totp", "webauthn"],
            "reason": "Lost phone",
            "identity_checked": True,
        }

    def test_a_material_manager_may_not(self, people: People, lost: User) -> None:
        with pytest.raises(AccessDenied):
            reset(people.mm_mad, lost)
        assert Authenticator.objects.filter(user=lost).count() == 2

    def test_a_revoked_administrator_may_not(
        self, make_user: Callable[..., User], lost: User
    ) -> None:
        former = make_user("former-admin@example.org")
        membership = grant(former, Role.GROUP_ADMIN)
        membership.revoked_at = membership.valid_from
        membership.save(update_fields=["revoked_at"])
        with pytest.raises(AccessDenied):
            reset(former, lost)
        assert Authenticator.objects.filter(user=lost).count() == 2

    def test_a_staff_account_is_a_superusers_to_reset(
        self, people: People, make_user: Callable[..., User]
    ) -> None:
        staff = make_user("ops2@example.org", is_staff=True)
        enroll_totp(staff)
        with pytest.raises(AccessDenied):
            reset(people.group_admin, staff, reason="Lost")
        assert Authenticator.objects.filter(user=staff).exists()

    @pytest.mark.parametrize("flag", ["is_staff", "is_superuser"])
    def test_from_a_session_not_even_a_superuser_resets_a_staff_account(
        self, make_user: Callable[..., User], flag: str
    ) -> None:
        """ADR-0030: a staff account's factors are reset only on the command line."""
        superuser = make_user("root@example.org", is_staff=True, is_superuser=True)
        grant(superuser, Role.GROUP_ADMIN)
        staff = make_user("ops2@example.org", **{flag: True})
        enroll_totp(staff)
        with pytest.raises(AccessDenied):
            reset(superuser, staff)
        assert Authenticator.objects.filter(user=staff).exists()
        assert not AuditEvent.objects.filter(action="user.mfa_reset").exists()

    @pytest.mark.parametrize(
        ("own", "reason", "checked", "code"),
        [
            (True, "Lost", True, "self"),
            (False, " ", True, "required"),
            (False, "Lost", False, "identity"),
        ],
        ids=["self", "no-reason", "identity-unchecked"],
    )
    def test_refused_for_oneself_without_a_reason_or_unchecked_identity(
        self, people: People, lost: User, own: bool, reason: str, checked: bool, code: str
    ) -> None:
        target = people.group_admin if own else lost
        with pytest.raises(ValidationError) as exc:
            reset(people.group_admin, target, reason=reason, identity_checked=checked)
        assert code in error_codes(exc.value)
        assert not AuditEvent.objects.filter(action="user.mfa_reset").exists()
        assert Authenticator.objects.filter(user=lost).count() == 2

    def test_the_same_form_twice_resets_once(self, people: People, lost: User) -> None:
        key = issue_key(people.group_admin.pk, services.MFA_RESET_OPERATION)
        first = reset(people.group_admin, lost, idempotency_key=key)
        enroll_totp(lost)  # the person enrolled again before the double tap arrived
        again = reset(people.group_admin, lost, idempotency_key=key)
        assert again.pk == first.pk
        assert Authenticator.objects.filter(user=lost).exists()
        assert AuditEvent.objects.filter(action="user.mfa_reset").count() == 1
        assert EmailOutbox.objects.filter(recipient_address=lost.email).count() == 1
        with pytest.raises(IdempotencyConflict):
            reset(people.group_admin, lost, idempotency_key=key, reason="Another")

    def test_a_replay_is_authorized_again(self, people: People, lost: User) -> None:
        key = issue_key(people.group_admin.pk, services.MFA_RESET_OPERATION)
        reset(people.group_admin, lost, idempotency_key=key)
        people.group_admin.memberships.update(revoked_at=lost.date_joined)
        with pytest.raises(AccessDenied):
            reset(people.group_admin, lost, idempotency_key=key)

    def test_the_security_log_names_the_account_and_the_administrator(
        self,
        people: People,
        lost: User,
        caplog: pytest.LogCaptureFixture,
        django_capture_on_commit_callbacks: Any,
    ) -> None:
        caplog.set_level(logging.INFO, logger="gmat.security")
        with django_capture_on_commit_callbacks(execute=True):
            reset(people.group_admin, lost)
        [line] = [r for r in caplog.records if r.name == "gmat.security"]
        assert line.security == {  # type: ignore[attr-defined]
            "event": "mfa_reset_by_admin",
            "user": str(lost.pk),
            "actor": str(people.group_admin.pk),
        }

    def test_a_refused_reset_logs_nothing(
        self,
        people: People,
        lost: User,
        caplog: pytest.LogCaptureFixture,
        django_capture_on_commit_callbacks: Any,
    ) -> None:
        caplog.set_level(logging.INFO, logger="gmat.security")
        with django_capture_on_commit_callbacks(execute=True), pytest.raises(ValidationError):
            reset(people.group_admin, lost, identity_checked=False)
        assert not [r for r in caplog.records if r.name == "gmat.security"]

    def test_a_restore_replays_it(self) -> None:
        """The action takes credentials away: ``apps.recovery.replay`` holds it (ADR-0016)."""
        assert "user.mfa_reset" in HANDLERS


class TestCommand:
    def test_resets_on_the_operators_authority(self, people: People, lost: User) -> None:
        out = StringIO()
        call_command(
            "reset_mfa",
            "--operator",
            people.group_admin.email,
            "--email",
            "TECH@example.org",
            "--reason",
            "Lost phone",
            "--identity-checked",
            stdout=out,
        )
        assert "tech@example.org" in out.getvalue()
        assert not Authenticator.objects.filter(user=lost).exists()
        event = AuditEvent.objects.get(action="user.mfa_reset")
        assert event.via == "cli:reset_mfa" and event.data["identity_checked"] is True

    @pytest.mark.parametrize(
        ("operator", "email", "reason", "checked", "message"),
        [
            ("group_admin", "unknown-8231@example.org", "Lost", True, "No account"),
            ("group_admin", "tech@example.org", " ", True, "reason"),
            ("group_admin", "tech@example.org", "Lost", False, "identity"),
            ("mm_mad", "tech@example.org", "Lost", True, "does not hold org.manage"),
        ],
        ids=["unknown", "no-reason", "identity-unchecked", "no-authority"],
    )
    def test_refusals_are_command_errors(
        self,
        people: People,
        lost: User,
        operator: str,
        email: str,
        reason: str,
        checked: bool,
        message: str,
    ) -> None:
        args = ["--operator", getattr(people, operator).email, "--email", email, "--reason", reason]
        if checked:
            args.append("--identity-checked")
        with translation.override("en"), pytest.raises(CommandError, match=message):
            call_command("reset_mfa", *args)
        assert Authenticator.objects.filter(user=lost).count() == 2

    def test_a_staff_target_is_refused_to_a_group_administrator(
        self, people: People, make_user: Callable[..., User]
    ) -> None:
        staff = make_user("ops2@example.org", is_staff=True)
        args = ["--operator", people.group_admin.email, "--email", staff.email, "--reason", "Lost"]
        with pytest.raises(CommandError, match="may not reset"):
            call_command("reset_mfa", *args, "--identity-checked")

    def test_a_superuser_resets_a_staff_account_on_the_command_line(
        self, make_user: Callable[..., User]
    ) -> None:
        superuser = make_user("root@example.org", is_staff=True, is_superuser=True)
        grant(superuser, Role.GROUP_ADMIN)
        staff = make_user("ops2@example.org", is_staff=True)
        enroll_totp(staff)
        args = ["--operator", superuser.email, "--email", staff.email, "--reason", "Lost"]
        call_command("reset_mfa", *args, "--identity-checked", stdout=StringIO())
        assert not Authenticator.objects.filter(user=staff).exists()


class TestRename:
    def test_an_administrator_names_an_account(self, people: People, lost: User) -> None:
        services.rename_user(principal_for(people.group_admin), lost.pk, full_name="  Ana   Ruiz ")
        lost.refresh_from_db()
        assert lost.full_name == "Ana Ruiz"
        event = AuditEvent.objects.get(action="user.renamed")
        assert event.actor == people.group_admin and event.object_id == lost.pk
        assert event.data == {"old": "", "new": "Ana Ruiz"}

    def test_own_name_is_allowed(self, people: People) -> None:
        me = people.group_admin
        services.rename_user(principal_for(me), me.pk, full_name="Group Admin Two")
        me.refresh_from_db()
        assert me.full_name == "Group Admin Two"

    def test_the_same_name_writes_nothing(self, people: People, lost: User) -> None:
        lost.full_name = "Ana Ruiz"
        lost.save(update_fields=["full_name"])
        services.rename_user(principal_for(people.group_admin), lost.pk, full_name="Ana Ruiz ")
        assert not AuditEvent.objects.filter(action="user.renamed").exists()

    @pytest.mark.parametrize(
        ("name", "code"), [("   ", "required"), ("x" * 151, "max_length")], ids=["blank", "long"]
    )
    def test_a_blank_or_long_name_is_refused(
        self, people: People, lost: User, name: str, code: str
    ) -> None:
        with pytest.raises(ValidationError) as exc:
            services.rename_user(principal_for(people.group_admin), lost.pk, full_name=name)
        assert error_codes(exc.value) == {code}
        lost.refresh_from_db()
        assert lost.full_name == ""

    @pytest.mark.parametrize("actor", ["mm_mad", "tech_mad", "operator"])
    def test_only_org_manage(self, people: People, lost: User, actor: str) -> None:
        with pytest.raises(AccessDenied):
            services.rename_user(principal_for(getattr(people, actor)), lost.pk, full_name="X")

    def test_a_staff_account_is_refused(self, people: People) -> None:
        with pytest.raises(AccessDenied):
            services.rename_user(
                principal_for(people.group_admin), people.operator.pk, full_name="Ops"
            )

    def test_the_field_and_the_limit_agree(self) -> None:
        assert User._meta.get_field("full_name").max_length == services.FULL_NAME_MAX_LENGTH
