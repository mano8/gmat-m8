"""Cross-cutting HTTP middleware: request ids, security headers, fail-closed cache errors."""

import logging
import re
import uuid
from collections.abc import Callable

from django.http import HttpRequest, HttpResponse, HttpResponseBase
from django.shortcuts import render
from django.template import TemplateDoesNotExist
from django.utils.cache import patch_cache_control, patch_vary_headers

from apps.core.cache import CacheUnavailable
from config.logging import request_id_var

logger = logging.getLogger(__name__)

GetResponse = Callable[[HttpRequest], HttpResponseBase]

REQUEST_ID_HEADER = "X-Request-ID"
_REQUEST_ID = re.compile(r"[A-Za-z0-9-]{8,64}")

# Token-bearing and pre-authentication pages (plan §5, Web): no Referer leaves them and no
# copy is cached, even for anonymous visitors. M2.6 adds invitation acceptance and
# email-change confirmation.
TOKEN_PAGE_URL_NAMES = frozenset(
    {
        "account_login",
        "account_logout",
        "account_inactive",
        "account_reauthenticate",
        "account_reset_password",
        "account_reset_password_done",
        "account_reset_password_from_key",
        "account_reset_password_from_key_done",
        "mfa_authenticate",
        "mfa_reauthenticate",
        "mfa_reauthenticate_webauthn",
    }
)

PERMISSIONS_POLICY = "camera=(), microphone=(), geolocation=(), payment=()"
CLEAR_SITE_DATA = '"cache", "storage"'


class RequestIdMiddleware:
    """Tags the request, its log records and its response with one id.

    A well-formed ``X-Request-ID`` from the proxy is kept so Caddy's and Django's logs line
    up; anything else is replaced by a fresh random id.
    """

    def __init__(self, get_response: GetResponse) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        incoming = request.META.get("HTTP_X_REQUEST_ID", "")
        request_id = incoming if _REQUEST_ID.fullmatch(incoming) else uuid.uuid4().hex
        token = request_id_var.set(request_id)
        try:
            response = self.get_response(request)
        finally:
            request_id_var.reset(token)
        response[REQUEST_ID_HEADER] = request_id
        return response


class SecurityHeadersMiddleware:
    """Caching and privacy headers that Django's ``SecurityMiddleware`` does not set.

    - every response to an authenticated user: ``Cache-Control: no-store, private`` and
      ``Vary: Cookie``;
    - token and pre-authentication pages: ``Referrer-Policy: no-referrer`` and ``no-store``,
      anonymous responses included;
    - logout: ``Clear-Site-Data``;
    - everywhere: ``Permissions-Policy``.
    """

    def __init__(self, get_response: GetResponse) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        response = self.get_response(request)
        match = getattr(request, "resolver_match", None)
        url_name = match.url_name if match is not None else None
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            patch_cache_control(response, no_store=True, private=True)
            patch_vary_headers(response, ["Cookie"])
        if url_name in TOKEN_PAGE_URL_NAMES:
            patch_cache_control(response, no_store=True, private=True)
            response["Referrer-Policy"] = "no-referrer"
        if url_name == "account_logout" and request.method == "POST":
            response["Clear-Site-Data"] = CLEAR_SITE_DATA
        response.setdefault("Permissions-Policy", PERMISSIONS_POLICY)
        return response


class CacheUnavailableMiddleware:
    """The security cache refused a write: answer 429 instead of failing open (plan §5)."""

    def __init__(self, get_response: GetResponse) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        return self.get_response(request)

    def process_exception(self, request: HttpRequest, exception: Exception) -> HttpResponse | None:
        if not isinstance(exception, CacheUnavailable):
            return None
        logger.warning("Security cache refused a write: %s", type(exception).__name__)
        try:
            return render(request, "429.html", status=429)
        except TemplateDoesNotExist:
            return HttpResponse("Too many requests.", status=429, content_type="text/plain")
