"""Session epoch and MFA enforcement (plan §3.6: session auth → epoch → MFA → access)."""

from collections.abc import Callable
from typing import Any

from django.contrib.auth import logout
from django.http import HttpRequest, HttpResponseBase

from apps.accounts.mfa import mfa_required_for, session_passed_mfa, step_up_response
from apps.accounts.sessions import session_is_current

GetResponse = Callable[[HttpRequest], HttpResponseBase]


class SessionGuardMiddleware:
    """Ends a session whose epoch is stale or whose lifetime is over.

    Runs right after ``AuthenticationMiddleware``: the rest of the request, including the
    view, sees an anonymous user.
    """

    def __init__(self, get_response: GetResponse) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        user = request.user
        if user.is_authenticated and not session_is_current(request, user):
            logout(request)
        return self.get_response(request)


class MFARequiredMiddleware:
    """Privileged URLs need a session that completed MFA (``apps.accounts.mfa``)."""

    def __init__(self, get_response: GetResponse) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        return self.get_response(request)

    def process_view(
        self, request: HttpRequest, view_func: Any, view_args: Any, view_kwargs: Any
    ) -> HttpResponseBase | None:
        if not request.user.is_authenticated or not mfa_required_for(request):
            return None
        if session_passed_mfa(request):
            return None
        return step_up_response(request)
