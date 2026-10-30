"""Fail-closed system checks (apps.core.checks)."""

import logging
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest
from cryptography.fernet import Fernet
from django.contrib import admin
from django.contrib.auth.models import Group
from django.test import override_settings
from django.utils.log import AdminEmailHandler

from apps.core import checks
from config.env import derived_fernet_key
from config.logging import RedactionFilter

MFA_KEY = Fernet.generate_key().decode()
SMTP: dict[str, Any] = {"BACKEND": "django.core.mail.backends.smtp.EmailBackend"}
GOOD_MAILERS: dict[str, dict[str, Any]] = {
    "default": {"BACKEND": checks.OUTBOX_BACKEND},
    "outbound": {"BACKEND": checks.SMTP_BACKEND, "OPTIONS": {"host": "smtp", "timeout": 10}},
}

GOOD_DEPLOY: dict[str, Any] = {
    "DEBUG": False,
    "SECRET_KEY": "Zq8vN2xLp4Rt7WmB9cYd3GhJ6KsFa1UeVo5XnTi0PbQrCwEyAz",
    "MFA_ENCRYPTION_KEYS": [MFA_KEY],
    "OUTBOX_ENCRYPTION_KEYS": [Fernet.generate_key().decode()],
    "ALLOWED_HOSTS": ["app.example.org"],
    "CSRF_TRUSTED_ORIGINS": ["https://app.example.org"],
    "PASSWORD_HASHERS": ["django.contrib.auth.hashers.Argon2PasswordHasher"],
    "MAILERS": GOOD_MAILERS,
}


def ids(messages: list[Any]) -> set[str]:
    return {message.id for message in messages}


class TestAlwaysOnChecks:
    def test_the_project_configuration_passes(self) -> None:
        assert checks.check_allauth(None) == []
        assert checks.check_security_cache(None) == []
        assert checks.check_logging(None) == []
        assert checks.check_admin_base_class(None) == []
        assert checks.check_idempotency_windows(None) == []
        assert checks.check_no_smtp_in_requests(None) == []

    @pytest.mark.parametrize("mailers", [{}, {"default": SMTP}])
    def test_requests_never_send_smtp(self, mailers: dict[str, Any]) -> None:
        with override_settings(MAILERS=mailers):
            assert ids(checks.check_no_smtp_in_requests(None)) == {"gmat.E007"}

    @pytest.mark.parametrize(
        ("max_age", "retention"), [(86_400, 86_400), (86_400, 3_600), (0, 3_600)]
    )
    def test_idempotency_records_must_outlive_their_keys(
        self, max_age: int, retention: int
    ) -> None:
        with override_settings(
            IDEMPOTENCY_KEY_MAX_AGE=max_age, IDEMPOTENCY_RECORD_RETENTION=retention
        ):
            assert ids(checks.check_idempotency_windows(None)) == {"gmat.E006"}

    def test_floor_matches_the_lock_check(self, load_script: Any) -> None:
        floors = load_script("check_security_floors")
        assert floors.FLOORS["django-allauth"] == checks.ALLAUTH_FLOOR

    def test_allauth_below_the_floor_is_refused(self) -> None:
        with mock.patch("allauth.__version__", "65.19.9"):
            assert "gmat.E001" in ids(checks.check_allauth(None))

    @pytest.mark.parametrize(
        "override",
        [
            {"ACCOUNT_PREVENT_ENUMERATION": False},
            {"ACCOUNT_LOGOUT_ON_GET": True},
            {"ACCOUNT_LOGIN_ON_PASSWORD_RESET": True},
            {"ACCOUNT_LOGIN_BY_CODE_ENABLED": True},
            {"MFA_TRUST_ENABLED": True},
            {"MFA_TOTP_TOLERANCE": 1},
            {"MFA_TOTP_INSECURE_BYPASS_CODE": "000000"},
            {"ALLAUTH_TRUSTED_PROXY_COUNT": 2},
        ],
    )
    def test_weakened_allauth_settings_are_refused(self, override: dict[str, Any]) -> None:
        with override_settings(**override):
            assert "gmat.E002" in ids(checks.check_allauth(None))

    def test_open_signup_is_refused(self) -> None:
        with mock.patch(
            "apps.accounts.adapters.AccountAdapter.is_open_for_signup", return_value=True
        ):
            assert "gmat.E002" in ids(checks.check_allauth(None))

    def test_another_cache_backend_is_refused(self) -> None:
        caches = {"default": {"BACKEND": "django.core.cache.backends.db.DatabaseCache"}}
        with override_settings(CACHES=caches):
            assert ids(checks.check_security_cache(None)) == {"gmat.E003"}

    def test_a_handler_without_redaction_is_refused(self) -> None:
        logging_config = {
            "filters": {"redact": {"()": checks.REDACTION_FILTER}},
            "handlers": {
                "plain": {"class": "logging.StreamHandler"},
                "mail": {
                    "class": "django.utils.log.AdminEmailHandler",
                    "filters": ["redact"],
                },
            },
        }
        # Read by the check only: changing settings.LOGGING would reconfigure real logging.
        with mock.patch.object(checks, "settings", SimpleNamespace(LOGGING=logging_config)):
            messages = checks.check_logging(None)
        assert ids(messages) == {"gmat.E004"}
        assert len(messages) == 2

    @pytest.fixture
    def email_handler(self) -> Iterator[logging.Logger]:
        logger = logging.getLogger("django.security")
        handler = AdminEmailHandler()
        logger.addHandler(handler)
        yield logger
        logger.removeHandler(handler)

    def test_an_email_handler_reaching_django_is_refused(
        self, email_handler: logging.Logger
    ) -> None:
        messages = checks.check_logging(None)
        assert ids(messages) == {"gmat.E004"}
        assert "AdminEmailHandler" in messages[0].msg

    def test_the_configured_handler_redacts(self) -> None:
        handlers = logging.getLogger("django").handlers
        assert any(
            any(isinstance(item, RedactionFilter) for item in handler.filters)
            for handler in handlers
        )

    def test_a_plain_model_admin_is_refused(self) -> None:
        class PlainGroupAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
            pass

        admin.site.register(Group, PlainGroupAdmin)
        try:
            assert ids(checks.check_admin_base_class(None)) == {"gmat.E005"}
        finally:
            admin.site.unregister(Group)


class TestDeploymentChecks:
    def test_a_good_production_configuration_passes(self) -> None:
        with override_settings(**GOOD_DEPLOY):
            assert checks.check_deployment(None) == []

    @pytest.mark.parametrize(
        ("override", "error"),
        [
            ({"DEBUG": True}, "gmat.E010"),
            ({"SECRET_KEY": "short"}, "gmat.E011"),
            ({"SECRET_KEY": "change-me-" + "x" * 60}, "gmat.E011"),
            ({"MFA_ENCRYPTION_KEYS": []}, "gmat.E012"),
            ({"MFA_ENCRYPTION_KEYS": ["not-a-key"]}, "gmat.E012"),
            ({"MFA_ENCRYPTION_KEYS": [derived_fernet_key("test")]}, "gmat.E012"),
            ({"ALLOWED_HOSTS": []}, "gmat.E013"),
            ({"ALLOWED_HOSTS": ["*"]}, "gmat.E013"),
            ({"CSRF_TRUSTED_ORIGINS": []}, "gmat.E014"),
            ({"CSRF_TRUSTED_ORIGINS": ["http://app.example.org"]}, "gmat.E014"),
            ({"SESSION_COOKIE_NAME": "sessionid"}, "gmat.E015"),
            ({"CSRF_COOKIE_SECURE": False}, "gmat.E015"),
            ({"PASSWORD_HASHERS": ["django.contrib.auth.hashers.MD5PasswordHasher"]}, "gmat.E016"),
            ({"PASSWORD_HASHERS": []}, "gmat.E016"),
            ({"MAILERS": {**GOOD_MAILERS, "default": SMTP}}, "gmat.E017"),
            (
                {"MAILERS": {**GOOD_MAILERS, "default": {"BACKEND": "x.Console"}}},
                "gmat.E017",
            ),
            ({"OUTBOX_ENCRYPTION_KEYS": []}, "gmat.E018"),
            ({"OUTBOX_ENCRYPTION_KEYS": [derived_fernet_key("outbox-test")]}, "gmat.E018"),
            ({"OUTBOX_ENCRYPTION_KEYS": [MFA_KEY]}, "gmat.E018"),
            ({"MAILERS": {"default": GOOD_MAILERS["default"]}}, "gmat.E019"),
            (
                {"MAILERS": {**GOOD_MAILERS, "outbound": {**SMTP, "OPTIONS": {}}}},
                "gmat.E019",
            ),
            (
                {
                    "MAILERS": {
                        **GOOD_MAILERS,
                        "outbound": {**SMTP, "OPTIONS": {"timeout": 30}},
                    }
                },
                "gmat.E019",
            ),
            ({"OUTBOX_SEND_DEADLINE": 10}, "gmat.E019"),
            ({"OUTBOX_ABANDON_GRACE": 5}, "gmat.E019"),
        ],
    )
    def test_unsafe_production_values_are_refused(
        self, override: dict[str, Any], error: str
    ) -> None:
        with override_settings(**{**GOOD_DEPLOY, **override}):
            assert error in ids(checks.check_deployment(None))
