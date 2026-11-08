from typing import Any

from django.conf import settings
from django.db import connection
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST, require_safe

from apps.core.http import is_htmx
from apps.core.preferences import PreferencesForm, parse

MAX_NEXT_LENGTH = 2048


@require_safe
@never_cache
def healthz(request: HttpRequest) -> HttpResponse:
    """Liveness: the process answers. It touches nothing else."""
    return JsonResponse({"status": "ok"})


@require_safe
@never_cache
def readyz(request: HttpRequest) -> HttpResponse:
    """Readiness: the database answers. Failures reveal nothing about the cause."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:  # any failure means "not ready"; details stay out of the response
        return JsonResponse({"status": "unavailable"}, status=503)
    return JsonResponse({"status": "ok"})


def _samesite() -> dict[str, Any]:
    return {"samesite": settings.UI_COOKIE_SAMESITE}


def _safe_next(request: HttpRequest) -> str:
    """The page to return to: a local path, or ``/`` (the language switcher's rule)."""
    target = request.POST.get("next", "")
    if (
        len(target) > MAX_NEXT_LENGTH
        or any(ord(char) < 32 for char in target)
        or not url_has_allowed_host_and_scheme(
            target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
        )
    ):
        return "/"
    return target


@require_POST
@never_cache
def ui_preferences(request: HttpRequest) -> HttpResponse:
    """Remember the theme, the left menu's state or a folded category (M8b.2, Q69).

    Writes nothing but the preference cookie: no service, no audit, no session. Open to anyone
    (the entrance pages have a theme switch too), CSRF-protected like every POST. An htmx
    request gets 204; a plain one is sent back with 303. A value outside the allowlist is
    refused with 400 and the cookie stays as it was.
    """
    form = PreferencesForm(request.POST)
    if not form.is_valid():
        return render(request, "400.html", status=400)
    updated = form.apply(parse(request.COOKIES.get(settings.UI_COOKIE_NAME)))
    if is_htmx(request):
        response = HttpResponse(status=204)
    else:
        response = HttpResponse(status=303)
        response["Location"] = _safe_next(request)
    value = updated.serialize()
    if value:
        response.set_cookie(
            settings.UI_COOKIE_NAME,
            value,
            max_age=settings.UI_COOKIE_AGE,
            secure=settings.UI_COOKIE_SECURE,
            httponly=settings.UI_COOKIE_HTTPONLY,
            **_samesite(),
        )
    elif settings.UI_COOKIE_NAME in request.COOKIES:
        response.delete_cookie(settings.UI_COOKIE_NAME, **_samesite())
    return response
