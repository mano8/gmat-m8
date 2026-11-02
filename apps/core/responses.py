"""Answers the screens give when a service refuses a command for a reason other than access."""

from collections.abc import Callable

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.forms import Form
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _

from apps.core.forms import error_text, form_error_text
from apps.core.idempotency import IdempotencyError
from apps.core.locking import RetryExhausted


def idempotency_failure(request: HttpRequest, exc: IdempotencyError) -> HttpResponse:
    """400 for a tampered or foreign form key, 409 for an expired or reused one (plan §4.8)."""
    status = exc.status_code
    return render(request, f"{status}.html", status=status)


def conflict(request: HttpRequest) -> HttpResponse:
    """The command kept conflicting with others (``RetryExhausted``): reload and retry."""
    return render(request, "409.html", status=409)


def perform(
    request: HttpRequest,
    call: Callable[[], object],
    done: str,
    to: str,
    *,
    failed_to: str = "",
) -> HttpResponse:
    """Run a service call: flash its outcome and go to ``to`` (``failed_to`` if it failed)."""
    try:
        call()
    except ValidationError as exc:
        messages.error(request, error_text(exc))
        return redirect(failed_to or to)
    except IdempotencyError as exc:
        return idempotency_failure(request, exc)
    except RetryExhausted:
        return conflict(request)
    messages.success(request, done)
    return redirect(to)


def invalid(request: HttpRequest, form: Form, to: str) -> HttpResponse:
    """A form that cannot be sent as it is: flash its errors and go back to ``to``."""
    messages.error(request, form_error_text(form) or _("Check the form."))
    return redirect(to)
