"""Security events in the log (``apps.accounts.security_log``; plan §5, ASVS 5.0 V16.3)."""

import json
import logging
from collections.abc import Callable
from typing import Any

import pytest
from allauth.mfa import signals as mfa_signals
from allauth.mfa.models import Authenticator
from django.contrib.auth.models import AnonymousUser
from django.http import HttpRequest, HttpResponse
from django.test import Client, RequestFactory
from django.urls import reverse

from apps.access.middleware import AccessContextMiddleware
from apps.accounts.models import User
from apps.accounts.security_log import account_hint, hsts_switches_off, warn_hsts_off
from config.logging import JsonFormatter, RedactionFilter, RequestIdFilter, user_id_var
from tests.org import People, client_for
from tests.support import PASSWORD, enroll_totp, mfa_login, password_login, totp_authenticator

pytestmark = pytest.mark.django_db


@pytest.fixture
def security(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.INFO, logger="gmat.security")
    return caplog


def events(caplog: pytest.LogCaptureFixture, name: str) -> list[dict[str, Any]]:
    return [
        record.security  # type: ignore[attr-defined]
        for record in caplog.records
        if record.name == "gmat.security" and record.security["event"] == name  # type: ignore[attr-defined]
    ]


class TestSignIn:
    def test_a_sign_in_names_the_account_and_how_it_authenticated(
        self, client: Client, make_user: Callable[..., User], security: pytest.LogCaptureFixture
    ) -> None:
        user = make_user("tech@example.org")
        password_login(client, user.email)
        [event] = events(security, "login")
        assert event == {
            "event": "login",
            "user": str(user.pk),
            "ip": "127.0.0.1",
            "methods": "password",
        }

    def test_a_sign_in_with_mfa_names_the_factor(
        self,
        client: Client,
        make_user: Callable[..., User],
        frozen_totp: int,
        security: pytest.LogCaptureFixture,
    ) -> None:
        user = make_user("mm@example.org")
        mfa_login(client, user.email, enroll_totp(user))
        [event] = events(security, "login")
        assert event["methods"] == "mfa:totp,password"

    def test_a_failed_sign_in_carries_a_hint_never_the_address_or_the_password(
        self, client: Client, make_user: Callable[..., User], security: pytest.LogCaptureFixture
    ) -> None:
        make_user("tech@example.org")
        password_login(client, "Tech@Example.org", "Wrong-but-long-9431")
        password_login(client, "nobody@example.org", "Wrong-but-long-9431")
        first, second = events(security, "login_failed")
        assert first["account"] == account_hint("tech@example.org") != second["account"]
        assert "user" not in first and first["ip"] == "127.0.0.1"
        assert all(record.levelno == logging.WARNING for record in security.records)
        logged = json.dumps(
            [event for record in security.records for event in [record.__dict__]], default=str
        )
        for secret in ("tech@example", "nobody@example", "Wrong-but-long-9431"):
            assert secret not in logged.lower() and secret not in logged

    def test_a_failed_mfa_code(
        self,
        client: Client,
        make_user: Callable[..., User],
        frozen_totp: int,
        security: pytest.LogCaptureFixture,
    ) -> None:
        user = make_user("mm@example.org")
        enroll_totp(user)
        password_login(client, user.email)
        client.post(reverse("mfa_authenticate"), {"code": "000000"})
        [event] = events(security, "mfa_failed")
        assert event["user"] == str(user.pk) and event["factor"] == "totp"

    def test_a_sign_out(
        self, client: Client, make_user: Callable[..., User], security: pytest.LogCaptureFixture
    ) -> None:
        user = make_user("tech@example.org")
        password_login(client, user.email)
        client.post(reverse("account_logout"))
        [event] = events(security, "logout")
        assert event["user"] == str(user.pk)


class TestCredentials:
    def test_a_password_change(
        self, client: Client, make_user: Callable[..., User], security: pytest.LogCaptureFixture
    ) -> None:
        user = make_user("tech@example.org")
        password_login(client, user.email)
        new = "A-new-long-passphrase-7731"
        client.post(
            reverse("account_change_password"),
            {"oldpassword": PASSWORD, "password1": new, "password2": new},
        )
        [event] = events(security, "password_changed")
        assert event["user"] == str(user.pk)
        assert new not in security.text

    @pytest.mark.parametrize(
        ("signal", "event"),
        [
            (mfa_signals.authenticator_added, "mfa_added"),
            (mfa_signals.authenticator_removed, "mfa_removed"),
            (mfa_signals.authenticator_reset, "mfa_reset"),
        ],
    )
    def test_second_factor_changes(
        self,
        make_user: Callable[..., User],
        security: pytest.LogCaptureFixture,
        signal: Any,
        event: str,
    ) -> None:
        user = make_user("mm@example.org")
        enroll_totp(user)
        request = RequestFactory().post("/accounts/2fa/")
        request.user = AnonymousUser()  # an admin's reset, say: not the account's own session
        signal.send(
            sender=Authenticator, request=request, user=user, authenticator=totp_authenticator(user)
        )
        [logged] = events(security, event)
        assert logged == {"event": event, "user": str(user.pk), "ip": "127.0.0.1", "factor": "totp"}


class TestLogLines:
    def test_a_line_carries_the_event_and_the_account_as_json(
        self, make_user: Callable[..., User]
    ) -> None:
        user = make_user("tech@example.org")
        record = logging.LogRecord(
            "gmat.security", logging.INFO, __file__, 1, "Security event %s", ("login",), None
        )
        record.security = {"event": "login", "user": str(user.pk)}
        token = user_id_var.set(str(user.pk))
        try:
            RequestIdFilter().filter(record)
        finally:
            user_id_var.reset(token)
        RedactionFilter().filter(record)
        line = json.loads(JsonFormatter().format(record))
        assert line["user"] == str(user.pk)
        assert line["security"] == {"event": "login", "user": str(user.pk)}
        assert line["message"] == "Security event login"

    def test_the_view_runs_with_the_signed_in_account_named_in_the_log(
        self, make_user: Callable[..., User]
    ) -> None:
        user = make_user("tech@example.org")
        seen: list[str | None] = []

        def view(request: HttpRequest) -> HttpResponse:
            seen.append(user_id_var.get())
            return HttpResponse()

        request = RequestFactory().get("/")
        request.user = user
        request.session = {}  # type: ignore[assignment]
        AccessContextMiddleware(view)(request)
        assert seen == [str(user.pk)]
        assert user_id_var.get() is None

    def test_a_refused_request_names_who_made_it(
        self, people: People, caplog: pytest.LogCaptureFixture
    ) -> None:
        handler = caplog.handler
        logger = logging.getLogger("django.request")
        logger.addHandler(handler)
        try:
            response = client_for(people.tech_mad).get(reverse("suppliers:list"))
        finally:
            logger.removeHandler(handler)
        assert response.status_code == 403
        refused = {record.user_id for record in caplog.records if record.name == "django.request"}  # type: ignore[attr-defined]
        assert refused == {str(people.tech_mad.pk)}


class TestHstsWarning:
    """M8.3, X3: production says so at startup while a switch is off; a deploy never fails."""

    def test_every_switch_that_is_off_is_named(
        self, security: pytest.LogCaptureFixture, settings: Any
    ) -> None:
        settings.HSTS_STARTUP_WARNING = True
        settings.SECURE_HSTS_SECONDS = 31536000
        settings.SECURE_HSTS_INCLUDE_SUBDOMAINS = False
        settings.SECURE_HSTS_PRELOAD = False
        warn_hsts_off()
        (record,) = security.records
        assert record.levelno == logging.WARNING
        assert record.security == {  # type: ignore[attr-defined]
            "event": "hsts_incomplete",
            "off": "DJANGO_HSTS_INCLUDE_SUBDOMAINS,DJANGO_HSTS_PRELOAD",
        }

    def test_nothing_to_say_when_all_are_on(
        self, security: pytest.LogCaptureFixture, settings: Any
    ) -> None:
        settings.HSTS_STARTUP_WARNING = True
        settings.SECURE_HSTS_SECONDS = 31536000
        settings.SECURE_HSTS_INCLUDE_SUBDOMAINS = True
        settings.SECURE_HSTS_PRELOAD = True
        assert hsts_switches_off() == []
        warn_hsts_off()
        assert not security.records

    def test_outside_production_it_stays_quiet(
        self, security: pytest.LogCaptureFixture, settings: Any
    ) -> None:
        settings.HSTS_STARTUP_WARNING = False
        warn_hsts_off()
        assert not security.records
