"""An administrator resets the second factors of an account that lost them (M8.3)."""

from collections.abc import Callable
from io import StringIO

import pytest
from allauth.mfa.models import Authenticator
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.access.exceptions import AccessDenied
from apps.access.roles import Role
from apps.accounts import services
from apps.accounts.models import User
from apps.audit.models import AuditEvent
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


class TestService:
    def test_a_group_administrator_removes_every_factor_and_ends_every_session(
        self, people: People, lost: User
    ) -> None:
        epoch = lost.session_epoch
        services.reset_user_mfa(principal_for(people.group_admin), lost.pk, reason=" Lost phone ")
        lost.refresh_from_db()
        assert not Authenticator.objects.filter(user=lost).exists()
        assert lost.session_epoch > epoch
        event = AuditEvent.objects.get(action="user.mfa_reset")
        assert event.object_id == lost.pk and event.actor == people.group_admin
        assert event.data == {"factors_removed": ["totp", "webauthn"], "reason": "Lost phone"}

    def test_a_material_manager_may_not(self, people: People, lost: User) -> None:
        with pytest.raises(AccessDenied):
            services.reset_user_mfa(principal_for(people.mm_mad), lost.pk, reason="Lost phone")
        assert Authenticator.objects.filter(user=lost).count() == 2

    def test_a_revoked_administrator_may_not(
        self, make_user: Callable[..., User], lost: User
    ) -> None:
        former = make_user("former-admin@example.org")
        membership = grant(former, Role.GROUP_ADMIN)
        membership.revoked_at = membership.valid_from
        membership.save(update_fields=["revoked_at"])
        with pytest.raises(AccessDenied):
            services.reset_user_mfa(principal_for(former), lost.pk, reason="Lost phone")
        assert Authenticator.objects.filter(user=lost).count() == 2

    def test_a_staff_account_is_a_superusers_to_reset(
        self, people: People, make_user: Callable[..., User]
    ) -> None:
        staff = make_user("ops2@example.org", is_staff=True)
        enroll_totp(staff)
        with pytest.raises(AccessDenied):
            services.reset_user_mfa(principal_for(people.group_admin), staff.pk, reason="Lost")
        assert Authenticator.objects.filter(user=staff).exists()

    @pytest.mark.parametrize(
        ("own", "reason", "code"), [(True, "Lost", "self"), (False, " ", "required")]
    )
    def test_refused_for_oneself_or_without_a_reason(
        self, people: People, lost: User, own: bool, reason: str, code: str
    ) -> None:
        target = people.group_admin if own else lost
        with pytest.raises(ValidationError) as exc:
            services.reset_user_mfa(principal_for(people.group_admin), target.pk, reason=reason)
        assert code in error_codes(exc.value)
        assert not AuditEvent.objects.filter(action="user.mfa_reset").exists()


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
            stdout=out,
        )
        assert "tech@example.org" in out.getvalue()
        assert not Authenticator.objects.filter(user=lost).exists()
        assert AuditEvent.objects.get(action="user.mfa_reset").via == "cli:reset_mfa"

    @pytest.mark.parametrize(
        ("operator", "email", "reason", "message"),
        [
            ("group_admin", "unknown-8231@example.org", "Lost", "No account"),
            ("group_admin", "tech@example.org", " ", "reason"),
            ("mm_mad", "tech@example.org", "Lost", "does not hold org.manage"),
        ],
    )
    def test_refusals_are_command_errors(
        self, people: People, lost: User, operator: str, email: str, reason: str, message: str
    ) -> None:
        args = ["--operator", getattr(people, operator).email, "--email", email, "--reason", reason]
        with pytest.raises(CommandError, match=message):
            call_command("reset_mfa", *args)
        assert Authenticator.objects.filter(user=lost).count() == 2

    def test_a_staff_target_is_refused_to_a_group_administrator(
        self, people: People, make_user: Callable[..., User]
    ) -> None:
        staff = make_user("ops2@example.org", is_staff=True)
        args = ["--operator", people.group_admin.email, "--email", staff.email, "--reason", "Lost"]
        with pytest.raises(CommandError, match="may not reset"):
            call_command("reset_mfa", *args)
