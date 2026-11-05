"""Session epoch and MFA enforcement (plan §3.6: session auth → epoch → MFA → access)."""

from collections.abc import Callable
from typing import Any

from django.contrib.auth import logout
from django.http import HttpRequest, HttpResponseBase

from apps.accounts.mfa import (
    mfa_required_for,
    must_generate_recovery_codes,
    new_recovery_codes_response,
    session_passed_mfa,
    step_up_response,
)
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
    """Privileged URLs need a session that completed MFA and, after a restore deleted the
    account's recovery codes, a new set of them (``apps.accounts.mfa``)."""

    def __init__(self, get_response: GetResponse) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        return self.get_response(request)

    def process_view(
        self, request: HttpRequest, view_func: Any, view_args: Any, view_kwargs: Any
    ) -> HttpResponseBase | None:
        if not request.user.is_authenticated or not mfa_required_for(request):
            return None
        if not session_passed_mfa(request):
            return step_up_response(request)
        if must_generate_recovery_codes(request):
            return new_recovery_codes_response(request)
        return None
