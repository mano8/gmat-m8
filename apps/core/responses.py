"""Answers the screens give when a service refuses a command for a reason other than access."""

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render

from apps.core.idempotency import IdempotencyError


def idempotency_failure(request: HttpRequest, exc: IdempotencyError) -> HttpResponse:
    """400 for a tampered or foreign form key, 409 for an expired or reused one (plan §4.8)."""
    status = exc.status_code
    return render(request, f"{status}.html", status=status)


def conflict(request: HttpRequest) -> HttpResponse:
    """The command kept conflicting with others (``RetryExhausted``): reload and retry."""
    return render(request, "409.html", status=409)
