"""Access errors and the HTTP status each one maps to (plan §3.5, S7)."""

from django.core.exceptions import PermissionDenied
from django.http import Http404


class AccessDenied(PermissionDenied):
    """In scope, but the principal may not do this (403)."""


class NotInScope(Http404):
    """The object is outside the principal's scope: answer as if it did not exist (404)."""


class StaleSession(AccessDenied):
    """The session predates a password, email or MFA change or a deactivation (403).

    ``AccessContextMiddleware`` also flushes the session, as plan §3.6 requires.
    """


class MFARequired(AccessDenied):
    """A privileged grant needs a session that completed MFA after the last elevation (403)."""


class TokenRejected(AccessDenied):
    """A token principal's token, target or initiator no longer checks out (403)."""


class PrincipalNotAllowed(TypeError):
    """A non-session principal was constructed outside its allowlisted modules."""


class MethodNotAllowed(Exception):
    """The view does not accept this HTTP method (405); ``allowed`` lists the ones it does."""

    def __init__(self, allowed: tuple[str, ...]) -> None:
        super().__init__(", ".join(allowed))
        self.allowed = allowed
