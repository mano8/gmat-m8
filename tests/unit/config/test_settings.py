"""Settings fail closed: each broken production environment refuses to load."""

import importlib
import sys
from collections.abc import Iterator

import pytest
from django.core.checks import run_checks
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

GOOD_ENV = {
    "DJANGO_SECRET_KEY": "Zq8vN2xLp4Rt7WmB9cYd3GhJ6KsFa1UeVo5XnTi0PbQrCwEyAz",
    "DJANGO_ALLOWED_HOSTS": "app.example.org",
    "DB_NAME": "gmat",
    "DB_USER": "gmat",
    "DB_PASSWORD": "pw",
    "DB_HOST": "db",
}


def _forget() -> None:
    """Drop cached settings modules so the next import re-reads the environment."""
    for name in ("config.settings.prod", "config.settings.base"):
        sys.modules.pop(name, None)


@pytest.fixture
def load_prod(monkeypatch: pytest.MonkeyPatch) -> Iterator[object]:
    """Import ``config.settings.prod`` freshly under a controlled environment."""

    def _load(**overrides: str | None) -> object:
        env = {**GOOD_ENV, **overrides}
        for name in [*GOOD_ENV, "DJANGO_DEBUG", "DJANGO_HSTS_SECONDS"]:
            monkeypatch.delenv(name, raising=False)
        for name, value in env.items():
            if value is not None:
                monkeypatch.setenv(name, value)
        _forget()
        return importlib.import_module("config.settings.prod")

    yield _load
    _forget()


def test_good_production_environment_loads(load_prod) -> None:  # type: ignore[no-untyped-def]
    settings = load_prod()
    assert settings.DEBUG is False
    assert settings.ALLOWED_HOSTS == ["app.example.org"]
    assert settings.SESSION_COOKIE_SECURE is True
    assert settings.CSRF_COOKIE_SECURE is True
    assert settings.SECURE_HSTS_SECONDS == 31536000


def test_good_production_environment_passes_deploy_checks(load_prod) -> None:  # type: ignore[no-untyped-def]
    prod = load_prod()
    names = [name for name in dir(prod) if name.isupper() and name != "DATABASES"]
    with override_settings(**{name: getattr(prod, name) for name in names}):
        assert run_checks(include_deployment_checks=True, databases=[]) == []


def test_health_endpoints_are_exempt_from_the_https_redirect(load_prod) -> None:  # type: ignore[no-untyped-def]
    prod = load_prod()
    assert prod.SECURE_SSL_REDIRECT is True
    assert prod.SECURE_REDIRECT_EXEMPT == [r"^healthz/$", r"^readyz/$"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"DJANGO_SECRET_KEY": None},
        {"DJANGO_SECRET_KEY": "short"},
        {"DJANGO_SECRET_KEY": "dev-" + "x" * 60},
        {"DJANGO_SECRET_KEY": "change-me-" + "x" * 60},
        {"DJANGO_ALLOWED_HOSTS": None},
        {"DJANGO_ALLOWED_HOSTS": "*"},
        {"DJANGO_ALLOWED_HOSTS": "app.example.org,*"},
        {"DJANGO_DEBUG": "true"},
        {"DB_PASSWORD": None},
        {"DB_HOST": None},
        {"DB_NAME": None},
        {"DB_USER": None},
        {"DJANGO_HSTS_SECONDS": "-1"},
    ],
)
def test_broken_production_environment_is_refused(load_prod, overrides) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ImproperlyConfigured):
        load_prod(**overrides)


def test_build_settings_need_no_database_or_secret() -> None:
    from config.settings import build

    assert build.DATABASES == {}
    assert build.DEBUG is False


def test_build_settings_load_without_any_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [*GOOD_ENV, "DB_PORT"]:
        monkeypatch.delenv(name, raising=False)
    _forget()
    sys.modules.pop("config.settings.build", None)
    build = importlib.import_module("config.settings.build")
    assert build.DATABASES == {}
    _forget()
    sys.modules.pop("config.settings.build", None)


def test_dev_settings_default_to_local_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("DJANGO_DEBUG", "DJANGO_SECRET_KEY", "DJANGO_ALLOWED_HOSTS"):
        monkeypatch.delenv(name, raising=False)
    _forget()
    sys.modules.pop("config.settings.dev", None)
    dev = importlib.import_module("config.settings.dev")
    assert dev.DEBUG is True
    assert "localhost" in dev.ALLOWED_HOSTS
    sys.modules.pop("config.settings.dev", None)


def test_base_uses_custom_user_and_argon2() -> None:
    from django.conf import settings

    assert settings.AUTH_USER_MODEL == "accounts.User"
    from config.settings import base

    assert base.PASSWORD_HASHERS == ["django.contrib.auth.hashers.Argon2PasswordHasher"]
    assert base.USE_TZ is True
