"""Principals: who a service acts for (plan §3.6).

M2.2 needs only the browser session. M2.3 completes ``SessionPrincipal`` (session epoch,
MFA time) and adds ``TokenPrincipal``, ``SystemPrincipal`` and ``OperatorPrincipal`` with
their restricted constructors.
"""

from dataclasses import dataclass
from uuid import UUID

from django.http import HttpRequest

from apps.audit.recorder import AuditIdentity
from apps.core.http import client_ip


@dataclass(frozen=True)
class SessionPrincipal:
    """An interactive user, as the session identified them."""

    user_id: UUID
    email: str
    ip: str | None = None

    @classmethod
    def from_request(cls, request: HttpRequest) -> SessionPrincipal:
        user = request.user
        if not user.is_authenticated:
            raise ValueError("A session principal needs an authenticated user")
        return cls(user_id=user.pk, email=user.get_username(), ip=client_ip(request))

    @property
    def audit_identity(self) -> AuditIdentity:
        return AuditIdentity(label=self.email, user_id=self.user_id, ip=self.ip)


type Principal = SessionPrincipal
