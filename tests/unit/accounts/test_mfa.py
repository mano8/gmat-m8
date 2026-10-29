"""MFA: secrets at rest, code reuse, session-level verification, cancelled logins, resets."""

import json
from collections.abc import Callable
from datetime import timedelta

import pytest
from allauth.account.internal.flows.login import AUTHENTICATION_METHODS_SESSION_KEY
from allauth.mfa.adapter import DefaultMFAAdapter
from allauth.mfa.models import Authenticator
from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
from allauth.mfa.totp.internal.auth import TOTP
from cryptography.fernet import Fernet, InvalidToken
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ImproperlyConfigured, PermissionDenied
from django.db import connection
from django.test import Client, RequestFactory, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts import signals
from apps.accounts.adapters import MFAAdapter
from apps.accounts.mfa import (
    keep_own_records,
    mfa_required_for,
    reset_mfa,
    session_mfa_at,
    session_passed_mfa,
    step_up_response,
)
from apps.accounts.models import User
from tests.support import (
    enroll_totp,
    mfa_login,
    password_login,
    totp_authenticator,
    totp_code,
)

pytestmark = pytest.mark.django_db


def raw_authenticator_data(authenticator: Authenticator) -> str:
    with connection.cursor() as cursor:
        cursor.execute("SELECT data::text FROM mfa_authenticator WHERE id = %s", [authenticator.pk])
        row = cursor.fetchone()
    assert row is not None
    return str(row[0])


def records(client: Client) -> list[dict[str, object]]:
    return list(client.session.get(AUTHENTICATION_METHODS_SESSION_KEY, []))


class TestSecretsAtRest:
    def test_allauth_default_adapter_stores_plaintext(self, make_user: Callable[..., User]) -> None:
        """Why the project adapter exists: the pinned default really stores plaintext."""
        user = make_user()
        with override_settings(MFA_ADAPTER="allauth.mfa.adapter.DefaultMFAAdapter"):
            secret = enroll_totp(user)
        assert DefaultMFAAdapter().encrypt(secret) == secret
        assert secret in raw_authenticator_data(totp_authenticator(user))

    def test_totp_secret_never_reaches_the_database_in_plaintext(
        self, make_user: Callable[..., User]
    ) -> None:
        user = make_user()
        secret = enroll_totp(user)
        raw = raw_authenticator_data(totp_authenticator(user))
        assert secret not in raw
        assert secret.lower() not in raw.lower()
        stored = json.loads(raw)["secret"]
        assert MFAAdapter().decrypt(stored) == secret

    def test_recovery_code_seed_is_encrypted(self, make_user: Callable[..., User]) -> None:
        user = make_user()
        codes = RecoveryCodes.activate(user)
        seed = MFAAdapter().decrypt(codes.instance.data["seed"])
        raw = raw_authenticator_data(codes.instance)
        assert seed not in raw
        assert codes.validate_code(codes.generate_codes()[0])

    def test_rotation_keeps_old_secrets_readable(self, settings: object) -> None:
        old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
        settings.MFA_ENCRYPTION_KEYS = [old]  # type: ignore[attr-defined]
        token = MFAAdapter().encrypt("JBSWY3DPEHPK3PXP")
        settings.MFA_ENCRYPTION_KEYS = [new, old]  # type: ignore[attr-defined]
        assert MFAAdapter().decrypt(token) == "JBSWY3DPEHPK3PXP"
        fresh = MFAAdapter().encrypt("JBSWY3DPEHPK3PXP")
        assert Fernet(new.encode()).decrypt(fresh.encode()) == b"JBSWY3DPEHPK3PXP"

    def test_no_key_configured_fails_closed(self, settings: object) -> None:
        settings.MFA_ENCRYPTION_KEYS = []  # type: ignore[attr-defined]
        with pytest.raises(ImproperlyConfigured):
            MFAAdapter().encrypt("JBSWY3DPEHPK3PXP")

    def test_a_secret_no_key_can_read_fails_closed(self, settings: object) -> None:
        token = MFAAdapter().encrypt("JBSWY3DPEHPK3PXP")
        settings.MFA_ENCRYPTION_KEYS = [Fernet.generate_key().decode()]  # type: ignore[attr-defined]
        with pytest.raises(InvalidToken):
            MFAAdapter().decrypt(token)


class TestTotpLogin:
    def test_password_alone_does_not_log_in_an_enrolled_user(
        self, client: Client, make_user: Callable[..., User]
    ) -> None:
        user = make_user()
        enroll_totp(user)
        response = password_login(client, user.email)
        assert response["Location"] == reverse("mfa_authenticate")
        assert "_auth_user_id" not in client.session

    def test_a_code_accepted_once_is_refused_the_second_time(
        self, client: Client, make_user: Callable[..., User], frozen_totp: int
    ) -> None:
        user = make_user()
        secret = enroll_totp(user)
        first = mfa_login(client, user.email, secret)
        assert first.status_code == 302
        assert first["Location"] == reverse("core:home")
        client.post(reverse("account_logout"))

        second = mfa_login(client, user.email, secret)
        assert second.status_code == 200
        assert second.context["form"].errors
        assert "_auth_user_id" not in client.session

    def test_reuse_is_refused_at_the_authenticator_too(
        self, make_user: Callable[..., User], frozen_totp: int
    ) -> None:
        user = make_user()
        secret = enroll_totp(user)
        totp = TOTP(totp_authenticator(user))
        assert totp.validate_code(totp_code(secret)) is True
        assert totp.validate_code(totp_code(secret)) is False


class TestSessionMfa:
    def test_completed_mfa_counts(
        self, client: Client, make_user: Callable[..., User], frozen_totp: int
    ) -> None:
        user = make_user()
        secret = enroll_totp(user)
        mfa_login(client, user.email, secret)
        request = RequestFactory().get("/")
        request.user = user
        request.session = client.session
        assert session_mfa_at(request) is not None
        assert session_passed_mfa(request) is True

    def test_enrolled_but_unused_mfa_does_not_count(
        self, client: Client, make_user: Callable[..., User]
    ) -> None:
        user = make_user()
        enroll_totp(user)
        client.force_login(user)
        request = RequestFactory().get("/")
        request.user = user
        request.session = client.session
        assert session_passed_mfa(request) is False

    def test_anonymous_has_no_mfa(self, client: Client) -> None:
        request = RequestFactory().get("/")
        request.user = AnonymousUser()
        request.session = client.session
        assert session_mfa_at(request) is None

    def test_mfa_before_a_privilege_elevation_does_not_count(
        self, client: Client, make_user: Callable[..., User], frozen_totp: int
    ) -> None:
        user = make_user()
        secret = enroll_totp(user)
        mfa_login(client, user.email, secret)
        user.privilege_elevated_at = timezone.now() + timedelta(seconds=60)
        request = RequestFactory().get("/")
        request.user = user
        request.session = client.session
        assert session_passed_mfa(request) is False

    def test_a_record_of_someone_elses_authenticator_never_counts(
        self, client: Client, make_user: Callable[..., User]
    ) -> None:
        user = make_user()
        other = make_user("other@example.org")
        enroll_totp(other)
        client.force_login(user)
        session = client.session
        session[AUTHENTICATION_METHODS_SESSION_KEY] = [
            {"method": "mfa", "at": 9e9, "id": totp_authenticator(other).pk, "type": "totp"}
        ]
        session.save()
        request = RequestFactory().get("/")
        request.user = user
        request.session = client.session
        assert session_passed_mfa(request) is False


class TestCancelledLoginAccountSwitching:
    """Alice starts a login on a shared device and abandons it; Bob then logs in there.

    allauth keeps Alice's pending login (and sends the login page to her MFA step) until it
    is cancelled or times out. Whichever way it ends, nothing of Alice's may reach Bob's
    session, and only an MFA record of Bob's own authenticator may count for him.
    """

    def assert_only_bob(self, client: Client, alice: User, bob: User) -> None:
        assert client.session["_auth_user_id"] == str(bob.pk)
        assert records(client), "Bob's own login is recorded"
        for record in records(client):
            assert record.get("email", bob.email) == bob.email
            assert record["method"] != "mfa" or record["id"] in set(
                Authenticator.objects.filter(user=bob).values_list("pk", flat=True)
            )
        assert all(alice.email not in json.dumps(record) for record in records(client))

    def test_bob_cancels_alices_pending_login_then_logs_in(
        self, client: Client, make_user: Callable[..., User]
    ) -> None:
        alice = make_user("alice@example.org")
        enroll_totp(alice)
        bob = make_user("bob@example.org")
        assert password_login(client, alice.email)["Location"] == reverse("mfa_authenticate")
        # The login page now leads to Alice's pending MFA step ...
        assert password_login(client, bob.email)["Location"] == reverse("mfa_authenticate")
        # ... which Bob cancels (the page's Cancel button logs out of the stage).
        client.post(reverse("account_logout"))
        assert password_login(client, bob.email)["Location"] == reverse("core:home")
        self.assert_only_bob(client, alice, bob)

    def test_alices_pending_login_times_out(
        self, client: Client, make_user: Callable[..., User], settings: object
    ) -> None:
        alice = make_user("alice@example.org")
        enroll_totp(alice)
        bob = make_user("bob@example.org")
        settings.ACCOUNT_LOGIN_TIMEOUT = 0  # type: ignore[attr-defined]
        password_login(client, alice.email)
        assert password_login(client, bob.email)["Location"] == reverse("core:home")
        self.assert_only_bob(client, alice, bob)

    def test_records_left_by_a_refused_login_are_discarded(
        self, client: Client, make_user: Callable[..., User]
    ) -> None:
        """A correct password for an inactive account records it, then refuses the login."""
        alice = make_user("alice@example.org", is_active=False)
        bob = make_user("bob@example.org")
        password_login(client, alice.email)
        assert "_auth_user_id" not in client.session
        assert password_login(client, bob.email)["Location"] == reverse("core:home")
        self.assert_only_bob(client, alice, bob)

    def test_a_leaked_mfa_record_of_a_is_never_credited_to_b(
        self, client: Client, make_user: Callable[..., User]
    ) -> None:
        alice = make_user("alice@example.org")
        enroll_totp(alice)
        bob = make_user("bob@example.org")
        session = client.session
        session[AUTHENTICATION_METHODS_SESSION_KEY] = [
            {"method": "password", "at": 1.0, "email": alice.email},
            {"method": "mfa", "at": 2.0, "id": totp_authenticator(alice).pk, "type": "totp"},
        ]
        session.save()
        password_login(client, bob.email)
        self.assert_only_bob(client, alice, bob)
        assert [record["method"] for record in records(client)] == ["password"]

    def test_bob_with_mfa_ends_up_with_only_his_records(
        self, client: Client, make_user: Callable[..., User], frozen_totp: int
    ) -> None:
        alice = make_user("alice@example.org")
        enroll_totp(alice)
        bob = make_user("bob@example.org")
        bob_secret = enroll_totp(bob)
        password_login(client, alice.email)
        client.post(reverse("account_logout"))
        mfa_login(client, bob.email, bob_secret)
        self.assert_only_bob(client, alice, bob)
        request = RequestFactory().get("/")
        request.user = bob
        request.session = client.session
        assert session_passed_mfa(request) is True

    def test_leftover_records_are_pruned_at_login(
        self, client: Client, make_user: Callable[..., User]
    ) -> None:
        """Defense in depth: the login signal keeps only the logged-in user's records."""
        alice = make_user("alice@example.org")
        enroll_totp(alice)
        bob = make_user("bob@example.org")
        session = client.session
        session[AUTHENTICATION_METHODS_SESSION_KEY] = [
            {"method": "password", "at": 1.0, "email": alice.email},
            {"method": "mfa", "at": 2.0, "id": totp_authenticator(alice).pk, "type": "totp"},
            {"method": "socialaccount", "at": 3.0},
        ]
        session.save()
        client.force_login(bob)
        assert records(client) == []

    def test_keep_own_records_with_no_records_is_a_no_op(
        self, client: Client, make_user: Callable[..., User]
    ) -> None:
        user = make_user()
        request = RequestFactory().get("/")
        request.session = client.session
        keep_own_records(request, user)
        assert AUTHENTICATION_METHODS_SESSION_KEY not in request.session


class TestMfaReset:
    def test_reset_removes_authenticators_and_ends_every_session(
        self, make_user: Callable[..., User], frozen_totp: int
    ) -> None:
        user = make_user()
        secret = enroll_totp(user)
        phone, laptop = Client(), Client()
        mfa_login(phone, user.email, secret)
        laptop.force_login(user)
        assert phone.get(reverse("core:home")).status_code == 200
        assert laptop.get(reverse("core:home")).status_code == 200
        epoch = user.session_epoch

        reset_mfa(user)

        assert user.session_epoch == epoch + 1
        assert not Authenticator.objects.filter(user=user).exists()
        assert phone.get(reverse("core:home")).status_code == 302
        assert laptop.get(reverse("core:home")).status_code == 302


class TestFactorRemoval:
    def test_removing_a_factor_ends_other_sessions_but_not_this_one(
        self, make_user: Callable[..., User]
    ) -> None:
        user = make_user()
        enroll_totp(user)
        this, other = Client(), Client()
        this.force_login(user)
        other.force_login(user)
        request = RequestFactory().post("/")
        request.user = user
        request.session = this.session
        signals.on_authenticator_removed(sender=Authenticator, request=request, user=user)
        request.session.save()
        assert this.get(reverse("core:home")).status_code == 200
        assert other.get(reverse("core:home")).status_code == 302

    def test_removal_without_a_request_ends_every_session(
        self, make_user: Callable[..., User]
    ) -> None:
        user = make_user()
        client = Client()
        client.force_login(user)
        signals.on_authenticator_removed(sender=Authenticator, request=None, user=user)
        assert client.get(reverse("core:home")).status_code == 302

    def test_privileged_accounts_keep_a_second_factor(self, make_user: Callable[..., User]) -> None:
        staff = make_user("ops@example.org", is_staff=True)
        enroll_totp(staff)
        totp = totp_authenticator(staff)
        assert MFAAdapter().can_delete_authenticator(totp) is False
        Authenticator.objects.create(user=staff, type=Authenticator.Type.WEBAUTHN, data={})
        assert MFAAdapter().can_delete_authenticator(totp) is True

    def test_other_accounts_may_remove_their_factor(self, make_user: Callable[..., User]) -> None:
        user = make_user()
        enroll_totp(user)
        assert MFAAdapter().can_delete_authenticator(totp_authenticator(user)) is True


class TestStepUp:
    def test_unsafe_methods_are_refused(self, make_user: Callable[..., User]) -> None:
        request = RequestFactory().post("/admin/")
        request.user = make_user()
        with pytest.raises(PermissionDenied):
            step_up_response(request)

    def test_enrolled_users_reauthenticate(self, make_user: Callable[..., User]) -> None:
        user = make_user()
        enroll_totp(user)
        request = RequestFactory().get("/admin/?q=1")
        request.user = user
        response = step_up_response(request)
        assert response["Location"].startswith(reverse("mfa_reauthenticate") + "?next=")

    def test_unenrolled_users_are_sent_to_enrollment(self, make_user: Callable[..., User]) -> None:
        request = RequestFactory().get("/admin/")
        request.user = make_user()
        response = step_up_response(request)
        assert response["Location"].startswith(reverse("mfa_index") + "?next=")

    def test_only_admin_urls_need_mfa_today(self, client: Client) -> None:
        request = RequestFactory().get("/")
        request.resolver_match = None
        assert mfa_required_for(request) is False
