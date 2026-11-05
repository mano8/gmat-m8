"""Security events in the log (plan §5, "Logging and secret redaction"; ASVS 5.0 V16.3).

Sign-ins and failed sign-ins, sign-outs, password changes and resets, second factors added,
removed or regenerated, and refused MFA codes: one JSON line each on the ``gmat.security``
logger, through the redaction filter like every record. An account is named by its id,
never by its address. A failed sign-in carries instead a keyed hash of the address that was
typed, so attempts on one account can be grouped without writing the address down. The
client address is the one Caddy observed (``apps.core.http.client_ip``), as in the audit.
"""

import logging
from typing import Any

from django.http import HttpRequest
from django.utils.crypto import salted_hmac

from apps.core.http import client_ip

logger = logging.getLogger("gmat.security")

# allauth's record of how the session authenticated (``record_authentication``).
AUTHENTICATION_RECORDS = "account_authentication_methods"
HINT_SALT = "gmat.security.account-hint"


def account_hint(address: str) -> str:
    """16 hex characters keyed with ``SECRET_KEY``: the same typed address, the same hint."""
    return salted_hmac(HINT_SALT, address.strip().lower(), algorithm="sha256").hexdigest()[:16]


def log_event(
    event: str,
    request: HttpRequest | None,
    user: Any = None,
    *,
    level: int = logging.INFO,
    **details: str,
) -> None:
    """One security event; ``details`` are short values (a factor type, a hint), never input."""
    fields: dict[str, str | None] = {"event": event, **details}
    if getattr(user, "pk", None) is not None:
        fields["user"] = str(user.pk)
    if request is not None:
        fields["ip"] = client_ip(request)
    logger.log(level, "Security event %s", event, extra={"security": fields})


def authentication_methods(request: HttpRequest | None) -> str:
    """How the session authenticated, e.g. ``mfa:totp,password``; never the address used."""
    session = getattr(request, "session", None)
    records = session.get(AUTHENTICATION_RECORDS, []) if session is not None else []
    names = {
        f"{item.get('method')}:{item['type']}" if item.get("type") else str(item.get("method"))
        for item in records
        if isinstance(item, dict)
    }
    return ",".join(sorted(names))


def _factor(authenticator: Any) -> str:
    return str(getattr(authenticator, "type", "") or "")


def on_logged_in(sender: Any, request: HttpRequest | None, user: Any, **kwargs: Any) -> None:
    log_event("login", request, user, methods=authentication_methods(request))


def on_login_failed(
    sender: Any, credentials: dict[str, Any], request: HttpRequest | None = None, **kwargs: Any
) -> None:
    address = str(credentials.get("email") or credentials.get("username") or "")
    hint = account_hint(address) if address else ""
    log_event("login_failed", request, level=logging.WARNING, account=hint)


def on_logged_out(sender: Any, request: HttpRequest | None, user: Any, **kwargs: Any) -> None:
    log_event("logout", request, user)


def on_password_changed(sender: Any, request: HttpRequest | None, user: Any, **kwargs: Any) -> None:
    log_event("password_changed", request, user)


def on_password_reset(sender: Any, request: HttpRequest | None, user: Any, **kwargs: Any) -> None:
    log_event("password_reset", request, user)


def on_factor_added(
    sender: Any, request: HttpRequest | None, user: Any, authenticator: Any = None, **kwargs: Any
) -> None:
    log_event("mfa_added", request, user, factor=_factor(authenticator))


def on_factor_removed(
    sender: Any, request: HttpRequest | None, user: Any, authenticator: Any = None, **kwargs: Any
) -> None:
    log_event("mfa_removed", request, user, factor=_factor(authenticator))


def on_factor_reset(
    sender: Any, request: HttpRequest | None, user: Any, authenticator: Any = None, **kwargs: Any
) -> None:
    log_event("mfa_reset", request, user, factor=_factor(authenticator))


def on_mfa_failed(
    sender: Any, request: HttpRequest | None, user: Any, authenticator: Any = None, **kwargs: Any
) -> None:
    log_event("mfa_failed", request, user, level=logging.WARNING, factor=_factor(authenticator))


def connect() -> None:
    """Connect every receiver (``AccountsConfig.ready``)."""
    from allauth.account import signals as account
    from allauth.mfa import signals as mfa
    from django.contrib.auth import signals as auth

    receivers = (
        (auth.user_logged_in, on_logged_in),
        (auth.user_login_failed, on_login_failed),
        (auth.user_logged_out, on_logged_out),
        (account.password_changed, on_password_changed),
        (account.password_reset, on_password_reset),
        (mfa.authenticator_added, on_factor_added),
        (mfa.authenticator_removed, on_factor_removed),
        (mfa.authenticator_reset, on_factor_reset),
        (mfa.authentication_failed, on_mfa_failed),
    )
    for signal, receiver in receivers:
        signal.connect(receiver, dispatch_uid=f"gmat-security-{receiver.__name__}")
