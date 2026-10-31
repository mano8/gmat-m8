"""``request.access`` and the HTTP side of access errors (plan §3.6).

``AccessContextMiddleware`` runs after the session epoch and MFA checks (plan §3.6: session
auth → epoch → MFA → access context). The context is cheap to create; it reads nothing
until a view, selector or template asks for a scope.
"""

from collections.abc import Callable
from functools import partial

from django.contrib.auth import logout
from django.http import HttpRequest, HttpResponseBase, HttpResponseNotAllowed

from apps.access.context import AccessContext
from apps.access.exceptions import MethodNotAllowed, StaleSession
from apps.access.principals import SessionPrincipal

GetResponse = Callable[[HttpRequest], HttpResponseBase]
REQUEST_ATTRIBUTE = "access"


def context_for_request(request: HttpRequest) -> AccessContext:
    """The access of the request's user, with the session's epoch and MFA time."""
    if not request.user.is_authenticated:
        return AccessContext.anonymous()
    return AccessContext(request.user, session=partial(SessionPrincipal.from_request, request))


def access_for(request: HttpRequest) -> AccessContext:
    """``request.access``, built on first use where the middleware did not run."""
    ctx = getattr(request, REQUEST_ATTRIBUTE, None)
    if not isinstance(ctx, AccessContext):
        ctx = context_for_request(request)
        setattr(request, REQUEST_ATTRIBUTE, ctx)
    return ctx


class AccessContextMiddleware:
    """Sets ``request.access``; answers ``MethodNotAllowed`` and flushes stale sessions."""

    def __init__(self, get_response: GetResponse) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        setattr(request, REQUEST_ATTRIBUTE, context_for_request(request))
        return self.get_response(request)

    def process_exception(
        self, request: HttpRequest, exception: Exception
    ) -> HttpResponseBase | None:
        if isinstance(exception, MethodNotAllowed):
            return HttpResponseNotAllowed(exception.allowed)
        if isinstance(exception, StaleSession):
            # The write was refused; the session itself is over (plan §3.6, step 2).
            logout(request)
        return None
