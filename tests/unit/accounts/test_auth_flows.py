"""allauth configuration: closed sign-up, no email views, no login by code, safe resets."""

import re
from collections.abc import Callable

import pytest
from allauth.account.forms import default_token_generator
from allauth.account.utils import user_pk_to_url_str
from django.core import mail
from django.test import Client
from django.urls import NoReverseMatch, reverse

from apps.accounts.adapters import AccountAdapter, PasswordResetTokenInvalid
from apps.accounts.models import User
from tests.support import PASSWORD, password_login

pytestmark = pytest.mark.django_db

NEW_PASSWORD = "another-long-password-2"


def reset_url(user: User) -> str:
    key = f"{user_pk_to_url_str(user)}-{default_token_generator.make_token(user)}"
    uidb36, _, token = key.partition("-")
    return reverse("account_reset_password_from_key", kwargs={"uidb36": uidb36, "key": token})


class TestClosedSurface:
    @pytest.mark.parametrize(
        "path",
        [
            "/accounts/signup/",
            "/accounts/email/",
            "/accounts/confirm-email/",
            "/accounts/confirm-email/some-key/",
            "/accounts/login/code/",
            "/accounts/login/code/confirm/",
            "/accounts/password/set/",
        ],
    )
    def test_closed_urls_do_not_exist(self, client: Client, path: str) -> None:
        assert client.get(path).status_code == 404
        assert client.post(path, {"email": "x@example.org"}).status_code == 404

    @pytest.mark.parametrize(
        "name",
        [
            "account_signup",
            "account_email",
            "account_email_verification_sent",
            "account_confirm_email",
            "account_request_login_code",
            "account_confirm_login_code",
            "account_set_password",
        ],
    )
    def test_closed_url_names_do_not_reverse(self, name: str) -> None:
        with pytest.raises(NoReverseMatch):
            reverse(name)

    def test_signup_is_closed_in_the_adapter_too(self) -> None:
        assert AccountAdapter().is_open_for_signup(None) is False  # type: ignore[arg-type]

    def test_no_user_is_created_by_any_signup_attempt(self, client: Client) -> None:
        client.post(
            "/accounts/signup/",
            {"email": "new@example.org", "password1": NEW_PASSWORD, "password2": NEW_PASSWORD},
        )
        assert not User.objects.filter(email="new@example.org").exists()


class TestLogin:
    def test_email_and_password_log_in(
        self, client: Client, make_user: Callable[..., User]
    ) -> None:
        user = make_user()
        response = password_login(client, "TECH@example.org")
        assert response.status_code == 302
        assert response["Location"] == reverse("core:home")
        assert client.get(reverse("core:home")).context["user"] == user

    def test_wrong_password_and_unknown_email_look_the_same(
        self, client: Client, make_user: Callable[..., User]
    ) -> None:
        make_user()
        wrong = password_login(client, "tech@example.org", "not-the-password-123")
        unknown = password_login(client, "nobody@example.org", "not-the-password-123")
        assert wrong.status_code == unknown.status_code == 200
        assert wrong.context["form"].errors == unknown.context["form"].errors

    def test_inactive_user_cannot_log_in(
        self, client: Client, make_user: Callable[..., User]
    ) -> None:
        make_user(is_active=False)
        response = password_login(client, "tech@example.org")
        assert "_auth_user_id" not in client.session
        assert response.status_code in {200, 302}

    def test_logout_needs_post(self, client: Client, make_user: Callable[..., User]) -> None:
        make_user()
        password_login(client, "tech@example.org")
        assert client.get(reverse("account_logout")).status_code == 200
        assert "_auth_user_id" in client.session
        response = client.post(reverse("account_logout"))
        assert response.status_code == 302
        assert "_auth_user_id" not in client.session


class TestPasswordReset:
    def test_request_does_not_reveal_whether_the_account_exists(
        self, client: Client, make_user: Callable[..., User]
    ) -> None:
        make_user()
        known = client.post(reverse("account_reset_password"), {"email": "tech@example.org"})
        unknown = client.post(reverse("account_reset_password"), {"email": "nobody@example.org"})
        assert known.status_code == unknown.status_code == 302
        assert known["Location"] == unknown["Location"]
        assert [message.to for message in mail.outbox] == [["tech@example.org"]]

    def test_inactive_accounts_receive_no_reset_email(
        self, client: Client, make_user: Callable[..., User]
    ) -> None:
        make_user(is_active=False)
        client.post(reverse("account_reset_password"), {"email": "tech@example.org"})
        assert mail.outbox == []

    def test_reset_redirects_to_a_keyless_url_and_never_logs_in(
        self, client: Client, make_user: Callable[..., User]
    ) -> None:
        user = make_user()
        client.post(reverse("account_reset_password"), {"email": "tech@example.org"})
        link = re.search(
            r"https?://[^/\s]+(/accounts/password/reset/key/\S+/)", str(mail.outbox[0].body)
        )
        assert link is not None
        response = client.get(link.group(1))
        assert response.status_code == 302
        keyless = response["Location"]
        assert keyless.endswith("-set-password/")
        key = link.group(1).rstrip("/").rsplit("-", 2)
        assert key[-1] not in keyless
        response = client.post(keyless, {"password1": NEW_PASSWORD, "password2": NEW_PASSWORD})
        assert response.status_code == 302
        assert response["Location"] == reverse("account_reset_password_from_key_done")
        assert "_auth_user_id" not in client.session
        user.refresh_from_db()
        assert user.check_password(NEW_PASSWORD)

    def test_a_used_token_fails_the_second_time(
        self, client: Client, make_user: Callable[..., User]
    ) -> None:
        user = make_user()
        url = reset_url(user)
        keyless = client.get(url)["Location"]
        client.post(keyless, {"password1": NEW_PASSWORD, "password2": NEW_PASSWORD})
        second = Client()
        response = second.get(url)
        assert response.status_code == 200
        assert response.context["token_fail"] is True

    def test_the_race_loser_sees_the_invalid_link_page(
        self, client: Client, make_user: Callable[..., User], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """allauth's check passes, the re-check under the row lock does not."""
        user = make_user()
        keyless = client.get(reset_url(user))["Location"]

        class LostRace:
            def check_token(self, user: User, token: str) -> bool:
                return False

        monkeypatch.setattr("apps.accounts.adapters.default_token_generator", LostRace())
        response = client.post(keyless, {"password1": NEW_PASSWORD, "password2": NEW_PASSWORD})
        assert response.status_code == 200
        assert response.context["token_fail"] is True
        user.refresh_from_db()
        assert user.check_password(PASSWORD)

    def test_reset_ends_existing_sessions(
        self, client: Client, make_user: Callable[..., User]
    ) -> None:
        user = make_user()
        other = Client()
        password_login(other, "tech@example.org")
        assert other.get(reverse("core:home")).status_code == 200
        user.refresh_from_db()  # the token hashes last_login, which that login changed
        keyless = client.get(reset_url(user))["Location"]
        client.post(keyless, {"password1": NEW_PASSWORD, "password2": NEW_PASSWORD})
        assert other.get(reverse("core:home")).status_code == 302

    def test_token_lifetime_is_one_hour(self, settings: object) -> None:
        assert settings.PASSWORD_RESET_TIMEOUT == 3600  # type: ignore[attr-defined]


class TestLockedRedemption:
    def test_valid_token_sets_the_password(self, make_user: Callable[..., User]) -> None:
        user = make_user()
        token = default_token_generator.make_token(user)
        AccountAdapter().redeem_password_reset(user, token, NEW_PASSWORD)
        user.refresh_from_db()
        assert user.check_password(NEW_PASSWORD)

    def test_invalid_token_is_refused(self, make_user: Callable[..., User]) -> None:
        user = make_user()
        with pytest.raises(PasswordResetTokenInvalid):
            AccountAdapter().redeem_password_reset(user, "1-bogus", NEW_PASSWORD)
        user.refresh_from_db()
        assert user.check_password(PASSWORD)

    def test_deactivated_account_is_refused_even_with_a_valid_token(
        self, make_user: Callable[..., User]
    ) -> None:
        user = make_user()
        token = default_token_generator.make_token(user)
        User.objects.filter(pk=user.pk).update(is_active=False)
        with pytest.raises(PasswordResetTokenInvalid):
            AccountAdapter().redeem_password_reset(user, token, NEW_PASSWORD)
