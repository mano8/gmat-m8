"""allauth adapters (plan §3.6 and §5).

This module is one of the allowlisted homes of non-session principals: the password-reset
redemption below runs as the password-reset ``TokenPrincipal`` of plan §3.6.
"""

from functools import lru_cache
from typing import TYPE_CHECKING, Any, cast

from allauth.account.adapter import DefaultAccountAdapter
from allauth.account.forms import default_token_generator
from allauth.account.internal.stagekit import clear_login
from allauth.mfa.adapter import DefaultMFAAdapter
from allauth.mfa.models import Authenticator
from cryptography.fernet import Fernet, MultiFernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, PermissionDenied
from django.http import HttpRequest

from apps.access.authorization import authorize_in_txn
from apps.access.exceptions import TokenRejected
from apps.access.policy import is_privileged
from apps.access.principals import TokenKind, TokenPrincipal
from apps.core.db import atomic_with_lock_timeout
from apps.core.http import client_ip
from apps.core.locking import LockOrder

if TYPE_CHECKING:
    from apps.accounts.models import User

# The one operation a password-reset token grants.
PASSWORD_RESET_OPERATION = "account.reset_password"  # noqa: S105  # nosec B105 - not a secret


class PasswordResetTokenInvalid(Exception):
    """The reset token no longer verifies under the user-row lock."""


class AccountAdapter(DefaultAccountAdapter):
    def is_open_for_signup(self, request: HttpRequest) -> bool:
        """Sign-up is closed: accounts only arrive through invitations (M2.6)."""
        return False

    def get_client_ip(self, request: HttpRequest) -> str:
        """The one client-IP helper, shared with the audit trail (``apps.core.http``)."""
        ip = client_ip(request)
        if ip is None:
            raise PermissionDenied("Unable to determine the client IP address")
        return ip

    def authenticate(self, request: HttpRequest, **credentials: Any) -> Any:
        """Start every login from a clean slate.

        A login attempt that was abandoned half-way (password accepted, MFA never given)
        leaves its pending state and authentication records in the session. A new login
        by anyone discards them, so they can never be credited to the next account.
        Reauthentication of a logged-in user keeps its session state.
        """
        if not request.user.is_authenticated:
            clear_login(request)
        return super().authenticate(request, **credentials)

    def redeem_password_reset(self, user: Any, key: str, password: str) -> None:
        """Set ``password`` only if ``key`` still verifies under the user-row lock.

        allauth checks the stateless token before showing the form, then sets the password
        without a lock, so two concurrent submissions could both succeed. Here the
        redemption is a ``TokenPrincipal``: ``authorize_in_txn`` locks the user row and the
        token is checked again under that lock. The first redemption changes the password
        hash, which invalidates the token for the second; setting it bumps
        ``session_epoch``. Inactive accounts are refused even with a token issued while
        they were active.
        """

        def check_token(target: User | None, locks: LockOrder) -> None:
            if (
                target is None
                or not target.is_active
                or not default_token_generator.check_token(target, key)
            ):
                raise TokenRejected("The password-reset link is no longer valid")

        principal = TokenPrincipal(
            kind=TokenKind.PASSWORD_RESET,
            operation=PASSWORD_RESET_OPERATION,
            target_user_id=user.pk,
            target_email=user.email,
            check_token=check_token,
        )
        try:
            with atomic_with_lock_timeout():
                # A principal with a target always yields that target's locked row.
                locked = cast(
                    "User",
                    authorize_in_txn(principal, PASSWORD_RESET_OPERATION, locks=LockOrder()),
                )
                self.set_password(locked, password)
        except TokenRejected as exc:
            raise PasswordResetTokenInvalid from exc


@lru_cache(maxsize=4)
def _multi_fernet(keys: tuple[str, ...]) -> MultiFernet:
    if not keys:
        raise ImproperlyConfigured("MFA_ENCRYPTION_KEYS is empty")
    return MultiFernet([Fernet(key.encode("ascii")) for key in keys])


class MFAAdapter(DefaultMFAAdapter):
    """Encrypts MFA secrets at rest with ``MFA_ENCRYPTION_KEYS`` (MultiFernet).

    allauth's default adapter stores TOTP secrets and recovery-code seeds in plaintext. The
    newest key comes first and encrypts; every listed key decrypts, so a rotation adds a
    key in front and re-encrypts. A value no key can decrypt raises ``InvalidToken``: the
    user cannot complete MFA until it is reset (runbook, M8.5).
    """

    def _fernet(self) -> MultiFernet:
        return _multi_fernet(tuple(settings.MFA_ENCRYPTION_KEYS))

    def encrypt(self, text: str) -> str:
        return self._fernet().encrypt(text.encode("utf-8")).decode("ascii")

    def decrypt(self, encrypted_text: str) -> str:
        return self._fernet().decrypt(encrypted_text.encode("ascii")).decode("utf-8")

    def can_delete_authenticator(self, authenticator: Authenticator) -> bool:
        """Privileged accounts must keep a second factor; others may remove theirs."""
        if not is_privileged(authenticator.user):
            return True
        return (
            Authenticator.objects.filter(user_id=authenticator.user_id)
            .exclude(pk=authenticator.pk)
            .exclude(type=Authenticator.Type.RECOVERY_CODES)
            .exists()
        )
