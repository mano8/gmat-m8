from django.contrib.auth.decorators import login_required
from django.db import connection
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe


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


@require_safe
@login_required
def home(request: HttpRequest) -> HttpResponse:
    """Landing page after login. The UI shell (M1.6) replaces its template."""
    return render(request, "core/home.html")
