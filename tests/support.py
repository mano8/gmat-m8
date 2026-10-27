"""Helpers shared by the test modules (fixtures live in ``tests/conftest.py``)."""

from typing import Any

from allauth.mfa.models import Authenticator
from allauth.mfa.totp.internal.auth import TOTP, format_hotp_value, generate_totp_secret, hotp_value
from django.test import Client
from django.urls import reverse

from apps.accounts.models import User

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
