"""HTTP hardening: headers, cookies, request ids, client IP, fail-closed rate limits."""

from collections.abc import Callable

import pytest
from allauth.account.forms import default_token_generator
from allauth.account.utils import user_pk_to_url_str
from django.core.cache import caches
from django.test import Client, RequestFactory, override_settings
from django.urls import reverse

from apps.accounts.models import User
from apps.core.cache import CacheFull, CacheUnavailable
from apps.core.http import client_ip
from apps.core.middleware import CacheUnavailableMiddleware
from tests.support import enroll_totp, password_login

pytestmark = pytest.mark.django_db

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
    "font-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; "
    "base-uri 'self'; object-src 'none'"
)


def reset_key_path(user: User) -> str:
    token = default_token_generator.make_token(user)
    return reverse(
        "account_reset_password_from_key",
        kwargs={"uidb36": user_pk_to_url_str(user), "key": token},
    )


class TestHeaders:
    def test_every_response_carries_the_baseline(self, client: Client) -> None:
        response = client.get(reverse("core:healthz"))
        assert response["Content-Security-Policy"] == CSP
        assert response["Permissions-Policy"] == (
            "camera=(), microphone=(), geolocation=(), payment=()"
        )
        assert response["X-Frame-Options"] == "DENY"
        assert response["X-Content-Type-Options"] == "nosniff"
        assert response["Referrer-Policy"] == "same-origin"
        assert response["Cross-Origin-Opener-Policy"] == "same-origin"

    @pytest.mark.parametrize(
        "name",
        [
            "account_login",
            "account_reset_password",
            "account_reset_password_done",
            "account_reset_password_from_key_done",
        ],
    )
    def test_anonymous_pre_auth_pages_are_private(self, client: Client, name: str) -> None:
        response = client.get(reverse(name))
        assert response.status_code == 200
        # A form page keeps same-origin: no-referrer would make browsers send `Origin: null`
        # on its POST, which CSRF refuses.
        assert response["Referrer-Policy"] == "same-origin"
        assert "no-store" in response["Cache-Control"]
        assert "Content-Security-Policy" in response

    def test_token_pages_are_private_even_when_invalid(
        self, client: Client, make_user: Callable[..., User]
    ) -> None:
        user = make_user()
        for path in (reset_key_path(user), reset_key_path(user).replace("-", "-x", 1)):
            response = client.get(path)
            assert response["Referrer-Policy"] == "no-referrer"
            assert "no-store" in response["Cache-Control"]
        keyless = client.get(reset_key_path(user))["Location"]
        response = client.get(keyless)
        # The key-less page shows the form; the secret is no longer in its address.
        assert response["Referrer-Policy"] == "same-origin"
        assert "no-store" in response["Cache-Control"]

    def test_mfa_step_is_private(self, client: Client, make_user: Callable[..., User]) -> None:
        user = make_user()
        enroll_totp(user)
        password_login(client, user.email)
        response = client.get(reverse("mfa_authenticate"))
        assert response.status_code == 200
        assert response["Referrer-Policy"] == "same-origin"
        assert "no-store" in response["Cache-Control"]

    @pytest.mark.parametrize(
        ("path", "secret"),
        [
            ("/accounts/password/reset/key/MQ-abc123def456/", True),
            ("/accounts/password/reset/key/MQ-set-password/", False),
            ("/accounts/password/reset/key/done/", False),
            ("/accounts/password/reset/key/", False),
            ("/accounts/password/reset/", False),
            ("/accounts/login/", False),
            ("/", False),
        ],
    )
    def test_only_a_url_with_a_token_is_no_referrer(self, path: str, secret: bool) -> None:
        from apps.core.middleware import path_carries_secret

        assert path_carries_secret(path) is secret

    def test_authenticated_pages_are_never_cached(
        self, client: Client, make_user: Callable[..., User]
    ) -> None:
        client.force_login(make_user())
        response = client.get(reverse("core:home"))
        assert response.status_code == 200
        assert "no-store" in response["Cache-Control"]
        assert "private" in response["Cache-Control"]
        assert "Cookie" in response["Vary"]

    def test_logout_clears_site_data(self, client: Client, make_user: Callable[..., User]) -> None:
        client.force_login(make_user())
        response = client.post(reverse("account_logout"))
        assert response["Clear-Site-Data"] == '"cache", "storage"'
        assert "no-store" in response["Cache-Control"]
        assert "Clear-Site-Data" not in client.get(reverse("account_logout"))

    def test_cookies_carry_the_host_prefix(
        self, client: Client, make_user: Callable[..., User]
    ) -> None:
        make_user()
        client.get(reverse("account_login"))
        csrf = client.cookies["__Host-csrftoken"]
        assert csrf["secure"] and csrf["path"] == "/" and not csrf["domain"]
        assert csrf["samesite"] == "Lax"
        password_login(client, "tech@example.org")
        session = client.cookies["__Host-sessionid"]
        assert session["secure"] and session["httponly"] and session["path"] == "/"
        assert not session["domain"]
        assert session["samesite"] == "Lax"


class TestRequestId:
    def test_a_fresh_id_is_set(self, client: Client) -> None:
        first = client.get(reverse("core:healthz"))["X-Request-ID"]
        second = client.get(reverse("core:healthz"))["X-Request-ID"]
        assert len(first) == 32 and first != second

    def test_a_well_formed_proxy_id_is_kept(self, client: Client) -> None:
        response = client.get(reverse("core:healthz"), HTTP_X_REQUEST_ID="caddy-1234-abcd")
        assert response["X-Request-ID"] == "caddy-1234-abcd"

    @pytest.mark.parametrize(
        "incoming",
        ["short", "x" * 65, "bad id\n", "a;b=c-123456", "abcdefgh\n", "abcdefgh\r\nX: 1"],
    )
    def test_a_malformed_id_is_replaced(self, client: Client, incoming: str) -> None:
        response = client.get(reverse("core:healthz"), HTTP_X_REQUEST_ID=incoming)
        assert response.status_code == 200
        assert response["X-Request-ID"] != incoming
        assert len(response["X-Request-ID"]) == 32


class TestClientIp:
    @pytest.mark.parametrize(
        ("meta", "expected"),
        [
            ({"REMOTE_ADDR": "10.0.0.2"}, "10.0.0.2"),
            ({"REMOTE_ADDR": "10.0.0.2", "HTTP_X_FORWARDED_FOR": "203.0.113.9"}, "203.0.113.9"),
            (
                {"REMOTE_ADDR": "10.0.0.2", "HTTP_X_FORWARDED_FOR": "6.6.6.6, 203.0.113.9"},
                "203.0.113.9",
            ),
            (
                {"REMOTE_ADDR": "10.0.0.2", "HTTP_X_FORWARDED_FOR": "203.0.113.9:4430"},
                "203.0.113.9",
            ),
            (
                {"REMOTE_ADDR": "10.0.0.2", "HTTP_X_FORWARDED_FOR": "[2001:db8::1]:443"},
                "2001:db8::1",
            ),
            ({"REMOTE_ADDR": "10.0.0.2", "HTTP_X_FORWARDED_FOR": "garbage"}, None),
            ({"REMOTE_ADDR": "10.0.0.2", "HTTP_X_FORWARDED_FOR": "[nope]:80"}, None),
            ({"REMOTE_ADDR": "10.0.0.2", "HTTP_X_FORWARDED_FOR": "example.org:80"}, None),
            ({"REMOTE_ADDR": ""}, None),
        ],
    )
    def test_trusts_only_the_proxy_appended_entry(
        self, meta: dict[str, str], expected: str | None
    ) -> None:
        request = RequestFactory().get("/")
        request.META.update(meta)
        assert client_ip(request) == expected

    def test_too_few_hops_for_the_trusted_count_is_refused(self) -> None:
        request = RequestFactory().get("/", HTTP_X_FORWARDED_FOR="203.0.113.9")
        with override_settings(ALLAUTH_TRUSTED_PROXY_COUNT=2):
            assert client_ip(request) is None

    def test_without_a_proxy_the_socket_address_counts(self) -> None:
        request = RequestFactory().get(
            "/", REMOTE_ADDR="192.0.2.4", HTTP_X_FORWARDED_FOR="203.0.113.9"
        )
        with override_settings(ALLAUTH_TRUSTED_PROXY_COUNT=0):
            assert client_ip(request) == "192.0.2.4"

    def test_forged_forwarded_for_does_not_change_the_bucket(
        self, client: Client, make_user: Callable[..., User]
    ) -> None:
        """10 failed logins per minute per IP: rotating a forged prefix does not help."""
        make_user()
        for attempt in range(10):
            response = password_login(
                client,
                f"nobody{attempt}@example.org",
                "wrong-password-123",
                HTTP_X_FORWARDED_FOR=f"6.6.6.{attempt}, 203.0.113.9",
            )
            assert "too many" not in response.content.decode().lower()
        response = password_login(
            client,
            "tech@example.org",
            "a-long-test-password-1",
            HTTP_X_FORWARDED_FOR="7.7.7.7, 203.0.113.9",
        )
        assert response.status_code == 200
        assert "_auth_user_id" not in client.session
        # A different real client is not affected.
        other = password_login(
            Client(), "tech@example.org", HTTP_X_FORWARDED_FOR="6.6.6.6, 198.51.100.7"
        )
        assert other.status_code == 302


class TestCacheFailsClosed:
    def test_a_full_cache_answers_429_on_login(
        self, client: Client, make_user: Callable[..., User]
    ) -> None:
        make_user()
        cache_settings = {
            "default": {
                "BACKEND": "apps.core.cache.SecurityDatabaseCache",
                "LOCATION": "django_cache",
                "OPTIONS": {"MAX_ENTRIES": 3},
            }
        }
        with override_settings(CACHES=cache_settings):
            cache = caches["default"]
            for number in range(3):
                cache.set(f"filler-{number}", number, 300)
            response = password_login(client, "tech@example.org")
            assert response.status_code == 429
            assert "_auth_user_id" not in client.session
            assert "no-store" in response["Cache-Control"]

    def test_other_exceptions_pass_through(self) -> None:
        middleware = CacheUnavailableMiddleware(lambda request: None)  # type: ignore[arg-type,return-value]
        request = RequestFactory().get("/")
        assert middleware.process_exception(request, ValueError()) is None
        response = middleware.process_exception(request, CacheUnavailable())
        assert response is not None and response.status_code == 429
        assert middleware.process_exception(request, CacheFull()).status_code == 429  # type: ignore[union-attr]

    def test_a_429_template_is_used_when_present(self, tmp_path: object, settings: object) -> None:
        from pathlib import Path

        directory = Path(str(tmp_path))
        (directory / "429.html").write_text("slow down", encoding="utf-8")
        templates = settings.TEMPLATES  # type: ignore[attr-defined]
        settings.TEMPLATES = [{**templates[0], "DIRS": [directory]}]  # type: ignore[attr-defined]
        middleware = CacheUnavailableMiddleware(lambda request: None)  # type: ignore[arg-type,return-value]
        response = middleware.process_exception(RequestFactory().get("/"), CacheFull())
        assert response is not None and response.content == b"slow down"
