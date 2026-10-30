"""Principals: who a service acts for (plan §3.6, "Principals other than a browser session").

- ``SessionPrincipal``: an interactive user, with the session's epoch and MFA time. Any
  module may build one from a request.
- ``TokenPrincipal``: one operation authorized by a token (invitation, email change,
  bootstrap, password reset).
- ``SystemPrincipal``: the worker, with a closed set of non-business capabilities.
- ``OperatorPrincipal``: a management command run with ``--operator <email>``.

The last three can only be constructed in their allowlisted modules
(``CONSTRUCTOR_MODULES``): the constructor checks the calling module and raises
``PrincipalNotAllowed`` anywhere else. ``check_access_rules.py`` (M2.4) enforces the same
list statically. ``authorize_in_txn`` accepts all four and runs the full check of each.
"""

import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from types import FrameType
from typing import TYPE_CHECKING, ClassVar
from uuid import UUID

from django.http import HttpRequest

from apps.access.exceptions import PrincipalNotAllowed
from apps.accounts.mfa import session_mfa_at
from apps.accounts.sessions import SESSION_EPOCH_KEY
from apps.audit.recorder import AuditIdentity
from apps.core.http import client_ip

if TYPE_CHECKING:
    from apps.accounts.models import User
    from apps.core.locking import LockOrder

# Class name -> modules (and their submodules) that may construct it (plan §3.6).
CONSTRUCTOR_MODULES: dict[str, frozenset[str]] = {
    "TokenPrincipal": frozenset({"apps.accounts.tokens", "apps.accounts.adapters"}),
    "SystemPrincipal": frozenset({"apps.notifications.worker"}),
    "OperatorPrincipal": frozenset({"apps.core.commands"}),
}


def _calling_module() -> str:
    """The first module on the stack outside this one (dataclass ``__init__`` included).

    A stack made only of this module would name this module, which is not allowlisted.
    """
    frame: FrameType = sys._getframe(1)
    while frame.f_back is not None and frame.f_globals.get("__name__") == __name__:
        frame = frame.f_back
    return str(frame.f_globals.get("__name__", ""))


def _check_constructor(cls: type) -> None:
    module = _calling_module()
    allowed = CONSTRUCTOR_MODULES[cls.__name__]
    if not any(module == name or module.startswith(f"{name}.") for name in allowed):
        raise PrincipalNotAllowed(f"{cls.__name__} cannot be constructed in {module or '?'}")


@dataclass(frozen=True)
class SessionPrincipal:
    """An interactive user, as the session identified them.

    ``session_epoch`` is the epoch the session was bound to at login; ``mfa_at`` is when
    this session last completed MFA (seconds since the epoch), or ``None``.
    """

    user_id: UUID
    email: str
    ip: str | None = None
    session_epoch: int | None = None
    mfa_at: float | None = None

    @classmethod
    def from_request(cls, request: HttpRequest) -> SessionPrincipal:
        user = request.user
        if not user.is_authenticated:
            raise ValueError("A session principal needs an authenticated user")
        epoch = request.session.get(SESSION_EPOCH_KEY)
        return cls(
            user_id=user.pk,
            email=user.get_username(),
            ip=client_ip(request),
            session_epoch=epoch if isinstance(epoch, int) else None,
            mfa_at=session_mfa_at(request),
        )

    @property
    def audit_identity(self) -> AuditIdentity:
        return AuditIdentity(label=self.email, user_id=self.user_id, ip=self.ip)


class TokenKind(StrEnum):
    INVITATION = "invitation"
    EMAIL_CHANGE = "email_change"
    BOOTSTRAP = "bootstrap"
    PASSWORD_RESET = "password_reset"  # noqa: S105  # nosec B105 - a kind, not a secret


# Locks and checks the token itself; ``authorize_in_txn`` has already locked the target and
# initiator ``User`` rows (in id order) and passes the target row (``None`` before a new
# account exists). Raises ``TokenRejected``.
type TokenCheck = Callable[[User | None, LockOrder], None]


@dataclass(frozen=True)
class TokenPrincipal:
    """Exactly one operation, on the authority of a token (plan §3.6).

    ``initiator_user_id`` is the administrator who issued a persistent token: they must
    still hold ``org.manage`` (and be a superuser for a staff target) when it is used (F71).
    """

    kind: TokenKind
    operation: str
    target_user_id: UUID | None
    target_email: str
    check_token: TokenCheck = field(repr=False, compare=False)
    token_id: UUID | None = None
    initiator_user_id: UUID | None = None
    ip: str | None = None

    def __post_init__(self) -> None:
        _check_constructor(type(self))

    @property
    def via(self) -> str:
        if self.kind in {TokenKind.INVITATION, TokenKind.EMAIL_CHANGE}:
            return f"{self.kind}:{self.token_id}"
        return str(self.kind)

    @property
    def audit_identity(self) -> AuditIdentity:
        return AuditIdentity(
            label=self.target_email, user_id=self.target_user_id, via=self.via, ip=self.ip
        )


@dataclass(frozen=True)
class SystemPrincipal:
    """The worker: no business capability, only ``outbox.send`` and ``housekeeping.*``."""

    NAMES: ClassVar[frozenset[str]] = frozenset({"system:worker"})
    CAPABILITIES: ClassVar[frozenset[str]] = frozenset({"outbox.send"})
    CAPABILITY_PREFIX: ClassVar[str] = "housekeeping."

    name: str = "system:worker"

    def __post_init__(self) -> None:
        _check_constructor(type(self))
        if self.name not in self.NAMES:
            raise ValueError(f"Unknown system actor {self.name!r}")

    def may(self, capability: str) -> bool:
        """The closed set: ``outbox.send`` and ``housekeeping.<task>``."""
        prefix = self.CAPABILITY_PREFIX
        return capability in self.CAPABILITIES or (
            capability.startswith(prefix) and capability != prefix
        )

    @property
    def audit_identity(self) -> AuditIdentity:
        return AuditIdentity(label=self.name, via=self.name)


@dataclass(frozen=True)
class OperatorPrincipal:
    """A management command acting on ``--operator``'s authority, for one capability.

    No session MFA is possible on a shell; the audit shows ``via=cli:<command>``.
    """

    user_id: UUID
    email: str
    command: str
    capability: str

    def __post_init__(self) -> None:
        _check_constructor(type(self))

    @property
    def via(self) -> str:
        return f"cli:{self.command}"

    @property
    def audit_identity(self) -> AuditIdentity:
        return AuditIdentity(label=self.email, user_id=self.user_id, via=self.via)


# Business services act for a user; the token and system principals have their own paths.
type Principal = SessionPrincipal | OperatorPrincipal
type AnyPrincipal = SessionPrincipal | OperatorPrincipal | TokenPrincipal | SystemPrincipal
