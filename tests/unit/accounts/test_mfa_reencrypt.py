"""Rotating the MFA key to the end: an old key can leave the list (M8.5, ``reencrypt_mfa``)."""

from collections.abc import Callable
from io import StringIO
from typing import Any

import pytest
from allauth.mfa.models import Authenticator
from cryptography.fernet import Fernet, InvalidToken
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client

from apps.access.exceptions import AccessDenied
from apps.accounts import services
from apps.accounts.adapters import MFAAdapter
from apps.accounts.models import User
from apps.audit.models import AuditEvent
from tests.org import People
from tests.support import enroll_totp, mfa_login, principal_for, totp_authenticator

pytestmark = pytest.mark.django_db

OLD, NEW = Fernet.generate_key().decode(), Fernet.generate_key().decode()


@pytest.fixture
def enrolled(make_user: Callable[..., User], settings: Any) -> tuple[User, str]:
    """An account whose TOTP secret and recovery seed were encrypted under the old key."""
    settings.MFA_ENCRYPTION_KEYS = [OLD]
    user = make_user("tech@example.org")
    secret = enroll_totp(user)
    Authenticator.objects.create(
        user=user,
        type=Authenticator.Type.RECOVERY_CODES,
        data={"seed": MFAAdapter().encrypt("seed-of-the-codes"), "used_mask": 0},
    )
    return user, secret


def test_every_secret_moves_and_the_old_key_can_leave(
    enrolled: tuple[User, str], people: People, settings: Any, client: Client, frozen_totp: int
) -> None:
    user, secret = enrolled
    settings.MFA_ENCRYPTION_KEYS = [NEW, OLD]
    done = services.reencrypt_mfa_secrets(principal_for(people.group_admin))
    # The fixture's other accounts were enrolled under the keys of the test settings.
    assert done.moved >= 2
    settings.MFA_ENCRYPTION_KEYS = [NEW]
    adapter = MFAAdapter()
    assert adapter.decrypt(totp_authenticator(user).data["secret"]) == secret
    seed = Authenticator.objects.get(user=user, type=Authenticator.Type.RECOVERY_CODES)
    assert adapter.decrypt(seed.data["seed"]) == "seed-of-the-codes"
    assert seed.data["used_mask"] == 0
    # And signing in works with the old key gone.
    assert mfa_login(client, user.email, secret).status_code == 302
    assert client.session.get("_auth_user_id") == str(user.pk)


def test_a_second_run_moves_nothing_and_says_so(
    enrolled: tuple[User, str], people: People, settings: Any
) -> None:
    settings.MFA_ENCRYPTION_KEYS = [NEW, OLD]
    principal = principal_for(people.group_admin)
    first = services.reencrypt_mfa_secrets(principal)
    second = services.reencrypt_mfa_secrets(principal)
    assert second.moved == 0
    assert second.already == first.moved + first.already
    assert second.unreadable == ()


def test_a_passkey_holds_no_secret_of_ours(
    make_user: Callable[..., User], people: People, settings: Any
) -> None:
    settings.MFA_ENCRYPTION_KEYS = [NEW]
    user = make_user("passkey@example.org")
    passkey = Authenticator.objects.create(
        user=user, type=Authenticator.Type.WEBAUTHN, data={"name": "phone", "credential": {}}
    )
    Authenticator.objects.exclude(pk=passkey.pk).delete()
    done = services.reencrypt_mfa_secrets(principal_for(people.group_admin))
    assert (done.moved, done.already, done.unreadable) == (0, 1, ())
    passkey.refresh_from_db()
    assert passkey.data == {"name": "phone", "credential": {}}


def test_what_no_listed_key_opens_is_reported_and_left_alone(
    enrolled: tuple[User, str], people: People, settings: Any
) -> None:
    user, _secret = enrolled
    settings.MFA_ENCRYPTION_KEYS = [NEW]  # the old key was lost
    before = totp_authenticator(user).data
    done = services.reencrypt_mfa_secrets(principal_for(people.group_admin))
    assert user.pk in done.unreadable
    assert totp_authenticator(user).data == before
    with pytest.raises(InvalidToken):
        MFAAdapter().decrypt(before["secret"])


def test_it_needs_org_manage(people: People) -> None:
    with pytest.raises(AccessDenied):
        services.reencrypt_mfa_secrets(principal_for(people.tech_mad))
    assert not AuditEvent.objects.filter(action="mfa.secrets_reencrypted").exists()


def test_it_is_audited_without_a_secret(
    enrolled: tuple[User, str], people: People, settings: Any
) -> None:
    settings.MFA_ENCRYPTION_KEYS = [NEW, OLD]
    done = services.reencrypt_mfa_secrets(principal_for(people.group_admin))
    event = AuditEvent.objects.get(action="mfa.secrets_reencrypted")
    assert event.data == {"moved": done.moved, "already": done.already, "unreadable": 0}


class TestCommand:
    def test_it_reports_what_it_did(
        self, enrolled: tuple[User, str], people: People, settings: Any
    ) -> None:
        settings.MFA_ENCRYPTION_KEYS = [NEW, OLD]
        out = StringIO()
        call_command("reencrypt_mfa", "--operator", people.group_admin.email, stdout=out)
        assert "moved to the newest key" in out.getvalue()
        assert AuditEvent.objects.get(action="mfa.secrets_reencrypted").via == "cli:reencrypt_mfa"

    def test_it_fails_when_an_account_cannot_be_opened(
        self, enrolled: tuple[User, str], people: People, settings: Any
    ) -> None:
        settings.MFA_ENCRYPTION_KEYS = [NEW]
        with pytest.raises(CommandError, match="No listed key opens"):
            call_command("reencrypt_mfa", "--operator", people.group_admin.email, stdout=StringIO())
