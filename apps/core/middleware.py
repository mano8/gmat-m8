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
from config.logging import TOKEN_PATH_PREFIXES, request_id_var

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

# allauth's key-less steps of a password reset: the key already sits in the session.
KEYLESS_SEGMENT_SUFFIX = "-set-password"
KEYLESS_SEGMENTS = frozenset({"done"})


def path_carries_secret(path: str) -> bool:
    """True for a URL whose path holds a token (not for the key-less page that follows it)."""
    for prefix in TOKEN_PATH_PREFIXES:
        start = path.find(prefix)
        if start == -1:
            continue
        segment = path[start + len(prefix) :].split("/", 1)[0]
        keyless = segment in KEYLESS_SEGMENTS or segment.endswith(KEYLESS_SEGMENT_SUFFIX)
        if segment and not keyless:
            return True
    return False


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
    - token and pre-authentication pages: ``no-store``, anonymous responses included;
    - a URL that carries a token: ``Referrer-Policy: no-referrer``. Pages with a form keep
      ``same-origin`` instead: under ``no-referrer`` browsers send ``Origin: null`` on a form
      POST, which Django's CSRF check refuses, so a login or reset form would never work.
      Such tokens are exchanged for a key-less URL before any form is shown;
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
            if path_carries_secret(request.path):
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


ERROR_PAGES = {405: "405.html", 409: "409.html", 429: "429.html"}


class ErrorPagesMiddleware:
    """Gives a bare 405, 409 or 429 answer a page that shows no internals.

    Django's 400, 403, 404 and 500 pages come from ``handler*`` and the templates of the same
    names; the other three statuses are returned by views and decorators as empty responses
    (``require_POST``, an idempotency conflict, a rate limit). A response that already has a
    body keeps it, and ``Allow`` and ``Retry-After`` are carried over.
    """

    def __init__(self, get_response: GetResponse) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        response = self.get_response(request)
        template = ERROR_PAGES.get(response.status_code)
        if template is None or response.streaming or getattr(response, "content", b"").strip():
            return response
        page = render(request, template, status=response.status_code)
        for header in ("Allow", "Retry-After"):
            if header in response:
                page[header] = response[header]
        return page
