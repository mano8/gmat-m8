"""Helpers shared by the test modules (fixtures live in ``tests/conftest.py``)."""

import time
from datetime import datetime, timedelta
from types import EllipsisType
from typing import Any

from allauth.mfa.models import Authenticator
from allauth.mfa.totp.internal.auth import TOTP, format_hotp_value, generate_totp_secret, hotp_value
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.access.principals import SessionPrincipal
from apps.access.roles import Role
from apps.accounts.models import User
from apps.notifications.crypto import unseal
from apps.notifications.models import EmailOutbox, Kind
from apps.organizations.models import Branch, Company, Membership

PASSWORD = "a-long-test-password-1"
# A fixed TOTP time step (see the ``frozen_totp`` fixture): codes are deterministic and
# never straddle a 30-second boundary.
TOTP_COUNTER = 59_000_000


def enroll_totp(user: User) -> str:
    """Enroll a TOTP authenticator for ``user`` and return its plaintext secret."""
    secret = generate_totp_secret()
    TOTP.activate(user, secret)
    return secret


def totp_code(secret: str, counter: int = TOTP_COUNTER) -> str:
    return format_hotp_value(hotp_value(secret, counter))


def totp_authenticator(user: User) -> Authenticator:
    authenticator: Authenticator = Authenticator.objects.get(
        user=user, type=Authenticator.Type.TOTP
    )
    return authenticator


def password_login(client: Client, email: str, password: str = PASSWORD, **extra: Any) -> Any:
    return client.post(reverse("account_login"), {"login": email, "password": password}, **extra)


def mfa_login(client: Client, email: str, secret: str, password: str = PASSWORD) -> Any:
    """Full login: the password, then the TOTP code of the frozen time step."""
    response = password_login(client, email, password)
    assert response.status_code == 302, response.content
    assert response["Location"] == reverse("mfa_authenticate")
    return client.post(reverse("mfa_authenticate"), {"code": totp_code(secret)})


def grant(
    user: User,
    role: Role,
    *,
    branch: Branch | None = None,
    company: Company | None = None,
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
    revoked_at: datetime | None = None,
) -> Membership:
    """A membership written directly (fixtures only; the app grants through services).

    ``valid_from`` defaults to an hour ago: the database clock of a test is the start of
    its transaction, which is earlier than Python's clock.
    """
    return Membership.objects.create(
        user=user,
        role=role,
        branch=branch,
        company=company,
        valid_from=valid_from or timezone.now() - timedelta(hours=1),
        valid_until=valid_until,
        revoked_at=revoked_at,
    )


def principal_for(user: User, *, mfa_at: float | EllipsisType | None = ...) -> SessionPrincipal:
    """The principal of a session that just logged in with MFA: the user's current epoch.

    ``mfa_at`` overrides when the session completed MFA (``None``: never).
    """
    epoch = User.objects.values_list("session_epoch", flat=True).get(pk=user.pk)
    completed = time.time() if mfa_at is ... else mfa_at
    return SessionPrincipal(
        user_id=user.pk,
        email=user.email,
        session_epoch=epoch,
        mfa_at=completed if isinstance(completed, float) else None,
    )


def authz_generation(user: User) -> int:
    return User.objects.values_list("authz_generation", flat=True).get(pk=user.pk)


def queued_security_mail() -> list[tuple[str, str, str]]:
    """``(address, subject, body)`` of each queued security email, oldest first, unsealed."""
    rows = EmailOutbox.objects.filter(kind=Kind.SECURITY).order_by("created_at", "id")
    return [(row.recipient_address, row.subject, unseal(row.body)) for row in rows]


def error_codes(exc: Any) -> set[str]:
    """The codes of a ``ValidationError`` (messages are translated; codes are not)."""
    error = exc.value if hasattr(exc, "value") else exc
    if hasattr(error, "error_dict"):
        return {item.code for items in error.error_dict.values() for item in items if item.code}
    return {item.code for item in error.error_list if item.code}
