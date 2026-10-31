"""Fail-closed system checks (plan §5, "Configuration").

The always-on checks guard settings that must hold in every environment: the allauth
floor and hardening, the security cache, log redaction and the admin base class. The
deployment checks run with ``manage.py check --deploy`` (image entrypoint, compose smoke
test) and refuse a production configuration that is unsafe, the mail setup included: the
outbox is the only default mailer, the worker's mailer is SMTP with bounded timeouts, and
``OUTBOX_ENCRYPTION_KEYS`` are real keys of their own.
"""

import logging
import re
from typing import Any

from django.conf import settings
from django.core.checks import CheckMessage, Error, register

from config.env import fernet_key_problems, is_placeholder, non_production_fernet_keys

ALLAUTH_FLOOR = "65.20.1"  # same floor as scripts/check_security_floors.py (plan §8.3)
SECURITY_CACHE = "apps.core.cache.SecurityDatabaseCache"
REDACTION_FILTER = "config.logging.RedactionFilter"
ARGON2 = "django.contrib.auth.hashers.Argon2PasswordHasher"
CHECKED_LOGGERS = ("django", "django.request", "django.server", "django.security")
OUTBOX_BACKEND = "apps.notifications.backends.OutboxEmailBackend"
SMTP_BACKEND = "django.core.mail.backends.smtp.EmailBackend"

# allauth settings whose value is part of the security baseline: name -> required value.
ALLAUTH_BASELINE: dict[str, Any] = {
    "ACCOUNT_PREVENT_ENUMERATION": True,
    "ACCOUNT_LOGOUT_ON_GET": False,
    "ACCOUNT_LOGIN_ON_PASSWORD_RESET": False,  # nosec B105
    "ACCOUNT_LOGIN_BY_CODE_ENABLED": False,
    "ACCOUNT_EMAIL_UNKNOWN_ACCOUNTS": False,
    "MFA_TRUST_ENABLED": False,
    "MFA_PASSKEY_LOGIN_ENABLED": False,
    "MFA_PASSKEY_SIGNUP_ENABLED": False,
    "MFA_TOTP_TOLERANCE": 0,
}


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version.split("+")[0])[:3])


@register()
def check_allauth(app_configs: Any, **kwargs: Any) -> list[CheckMessage]:
    import allauth

    errors: list[CheckMessage] = []
    if _version_key(allauth.__version__) < _version_key(ALLAUTH_FLOOR):
        errors.append(
            Error(
                f"django-allauth {allauth.__version__} is below the security floor {ALLAUTH_FLOOR}",
                id="gmat.E001",
            )
        )
    for name, required in ALLAUTH_BASELINE.items():
        if getattr(settings, name, None) != required:
            errors.append(Error(f"{name} must be {required!r}", id="gmat.E002"))
    if getattr(settings, "ALLAUTH_TRUSTED_PROXY_COUNT", 0) not in {0, 1}:
        errors.append(Error("ALLAUTH_TRUSTED_PROXY_COUNT must be 1 (Caddy) or 0", id="gmat.E002"))
    if getattr(settings, "MFA_TOTP_INSECURE_BYPASS_CODE", None) is not None:
        errors.append(Error("MFA_TOTP_INSECURE_BYPASS_CODE must not be set", id="gmat.E002"))
    from allauth.account.adapter import get_adapter

    if get_adapter().is_open_for_signup(None):  # type: ignore[arg-type]
        errors.append(Error("Sign-up must be closed (invitations only)", id="gmat.E002"))
    return errors


@register()
def check_security_cache(app_configs: Any, **kwargs: Any) -> list[CheckMessage]:
    backend = settings.CACHES.get("default", {}).get("BACKEND")
    if backend != SECURITY_CACHE:
        return [Error(f"The default cache must be {SECURITY_CACHE}", id="gmat.E003")]
    return []


def _effective_handlers(logger: logging.Logger) -> list[logging.Handler]:
    handlers: list[logging.Handler] = []
    current: logging.Logger | None = logger
    while current is not None:
        handlers.extend(current.handlers)
        if not current.propagate:
            break
        current = current.parent
    return handlers


@register()
def check_logging(app_configs: Any, **kwargs: Any) -> list[CheckMessage]:
    """Every configured handler redacts; no ``AdminEmailHandler`` reaches Django's loggers.

    The redaction rule is checked on ``settings.LOGGING`` (what deployments configure); the
    runtime walk only looks for email handlers, since test runners attach their own
    capture handlers at runtime.
    """
    from django.utils.log import AdminEmailHandler

    errors: list[CheckMessage] = []
    config = getattr(settings, "LOGGING", {}) or {}
    filters = config.get("filters", {})
    redacting = {name for name, spec in filters.items() if spec.get("()") == REDACTION_FILTER}
    for name, handler in config.get("handlers", {}).items():
        if not redacting.intersection(handler.get("filters", [])):
            errors.append(
                Error(f"Logging handler {name!r} lacks the redaction filter", id="gmat.E004")
            )
        if handler.get("class", "").endswith("AdminEmailHandler"):
            errors.append(Error(f"Logging handler {name!r} sends email", id="gmat.E004"))
    for logger_name in CHECKED_LOGGERS:
        for handler in _effective_handlers(logging.getLogger(logger_name)):
            if isinstance(handler, AdminEmailHandler):
                errors.append(
                    Error(f"Logger {logger_name!r} reaches an AdminEmailHandler", id="gmat.E004")
                )
    return errors


@register()
def check_admin_base_class(app_configs: Any, **kwargs: Any) -> list[CheckMessage]:
    from django.contrib import admin

    from apps.core.admin import ServiceBackedModelAdmin

    return [
        Error(
            f"{type(model_admin).__name__} for {model._meta.label} is not built on "
            "ServiceBackedModelAdmin",
            id="gmat.E005",
        )
        for model, model_admin in admin.site._registry.items()
        if not isinstance(model_admin, ServiceBackedModelAdmin)
    ]


@register()
def check_idempotency_windows(app_configs: Any, **kwargs: Any) -> list[CheckMessage]:
    """A record must outlive its key, or a purge could turn a replay into a new command."""
    max_age = int(getattr(settings, "IDEMPOTENCY_KEY_MAX_AGE", 0))
    retention = int(getattr(settings, "IDEMPOTENCY_RECORD_RETENTION", 0))
    if max_age <= 0 or retention <= max_age:
        return [
            Error(
                "IDEMPOTENCY_RECORD_RETENTION must be longer than IDEMPOTENCY_KEY_MAX_AGE (> 0)",
                id="gmat.E006",
            )
        ]
    return []


def _mailer_backend(alias: str) -> str | None:
    backend = getattr(settings, "MAILERS", {}).get(alias, {}).get("BACKEND")
    return backend if isinstance(backend, str) else None


@register()
def check_no_smtp_in_requests(app_configs: Any, **kwargs: Any) -> list[CheckMessage]:
    """Requests send through the default mailer: it must never be SMTP (plan §5)."""
    backend = _mailer_backend("default")
    if backend is None or backend == SMTP_BACKEND:
        return [
            Error(
                "MAILERS['default'] must queue to the outbox, never send SMTP from a request",
                id="gmat.E007",
            )
        ]
    return []


def _secret_errors() -> list[CheckMessage]:
    errors: list[CheckMessage] = []
    if settings.DEBUG:
        errors.append(Error("DEBUG must be off", id="gmat.E010"))
    secret_key = str(getattr(settings, "SECRET_KEY", ""))
    if len(secret_key) < 50 or is_placeholder(secret_key):
        errors.append(Error("SECRET_KEY is missing, weak or a placeholder", id="gmat.E011"))
    refused = non_production_fernet_keys()
    mfa_keys = list(getattr(settings, "MFA_ENCRYPTION_KEYS", []))
    errors.extend(
        Error(f"MFA_ENCRYPTION_KEYS: {problem}", id="gmat.E012")
        for problem in fernet_key_problems(mfa_keys, refused=refused)
    )
    outbox_keys = list(getattr(settings, "OUTBOX_ENCRYPTION_KEYS", []))
    errors.extend(
        Error(f"OUTBOX_ENCRYPTION_KEYS: {problem}", id="gmat.E018")
        for problem in fernet_key_problems(outbox_keys, refused=refused)
    )
    if set(outbox_keys) & set(mfa_keys):
        errors.append(Error("OUTBOX_ENCRYPTION_KEYS must not reuse an MFA key", id="gmat.E018"))
    return errors


def _host_errors() -> list[CheckMessage]:
    errors: list[CheckMessage] = []
    hosts = list(settings.ALLOWED_HOSTS)
    if not hosts or "*" in hosts:
        errors.append(Error("ALLOWED_HOSTS is empty or has a wildcard", id="gmat.E013"))
    origins = list(getattr(settings, "CSRF_TRUSTED_ORIGINS", []))
    if not origins or not all(origin.startswith("https://") for origin in origins):
        errors.append(Error("CSRF_TRUSTED_ORIGINS must list https origins", id="gmat.E014"))
    return errors


def _cookie_errors() -> list[CheckMessage]:
    cookies_ok = (
        settings.SESSION_COOKIE_NAME.startswith("__Host-")
        and settings.CSRF_COOKIE_NAME.startswith("__Host-")
        and settings.SESSION_COOKIE_SECURE
        and settings.CSRF_COOKIE_SECURE
    )
    if cookies_ok:
        return []
    return [Error("Session and CSRF cookies must be Secure __Host- cookies", id="gmat.E015")]


def _hasher_and_mail_errors() -> list[CheckMessage]:
    errors: list[CheckMessage] = []
    if not settings.PASSWORD_HASHERS or settings.PASSWORD_HASHERS[0] != ARGON2:
        errors.append(Error("Argon2 must be the first password hasher", id="gmat.E016"))
    if _mailer_backend("default") != OUTBOX_BACKEND:
        errors.append(
            Error(
                f"Production mail goes through the outbox: MAILERS['default'] = {OUTBOX_BACKEND}",
                id="gmat.E017",
            )
        )
    return errors


def _outbound_errors() -> list[CheckMessage]:
    """The worker's mailer: SMTP, every socket operation bounded, inside the deadline."""
    outbound = getattr(settings, "MAILERS", {}).get("outbound", {})
    timeout = outbound.get("OPTIONS", {}).get("timeout")
    deadline = int(getattr(settings, "OUTBOX_SEND_DEADLINE", 0))
    problems = []
    if outbound.get("BACKEND") != SMTP_BACKEND:
        problems.append(f"MAILERS['outbound'] must be {SMTP_BACKEND}")
    if not isinstance(timeout, int | float) or not 0 < timeout <= settings.OUTBOX_SMTP_TIMEOUT:
        problems.append(
            f"MAILERS['outbound'] needs a timeout of at most {settings.OUTBOX_SMTP_TIMEOUT} s"
        )
    elif deadline <= timeout:
        problems.append("OUTBOX_SEND_DEADLINE must be longer than one operation's timeout")
    if int(getattr(settings, "OUTBOX_ABANDON_GRACE", 0)) < 30:
        problems.append("OUTBOX_ABANDON_GRACE must be at least 30 s")
    return [Error(problem, id="gmat.E019") for problem in problems]


@register(deploy=True)
def check_deployment(app_configs: Any, **kwargs: Any) -> list[CheckMessage]:
    return [
        *_secret_errors(),
        *_host_errors(),
        *_cookie_errors(),
        *_hasher_and_mail_errors(),
        *_outbound_errors(),
    ]
