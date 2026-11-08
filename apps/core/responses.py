"""How the screens answer a POST-only action, plainly or from a dialog (M8b.3).

A plain POST (no JavaScript, or a form outside a dialog) keeps the answers of every screen so
far: the outcome is flashed and the browser is redirected. A POST that htmx sends from a
dialog gets answers htmx can use without swapping a whole page into the dialog:

- **success**: ``204`` with ``HX-Redirect`` to the same place a plain POST is redirected to.
  htmx navigates the whole page, which shows the flash. Never a redirect: htmx would follow it
  and swap the page it lands on into the dialog;
- **invalid** (the form's errors, or a service refusal shown on the form): ``422`` with the
  form's fragment and its errors, swapped into the open dialog (``responseHandling`` in
  ``base.html``). The form keeps its idempotency key: a key that produced nothing may be sent
  again. A key bound to a result is never shown again with another payload: a success
  navigates away, and the same key with other data is refused with 409;
- **409** (a stale ``seen_version``, a key reused with other data, a command that kept
  conflicting): ``components/dialog_problem.html``, swapped into the dialog, with the way out;
- **400, 403, 404, a redirect to sign in**: their usual answers. htmx does not swap them;
  ``app.js`` shows the dialog's own message, or navigates the whole page to the sign-in page.

Every answer that depends on ``HX-Request`` says ``Vary: HX-Request``. Authorization is the
views' and services' as before; nothing here decides who may do what.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.forms import Form
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render, resolve_url
from django.utils.cache import patch_vary_headers
from django.utils.translation import gettext as _

from apps.core.forms import add_service_errors, error_text, form_error_text
from apps.core.http import is_htmx
from apps.core.idempotency import IdempotencyError
from apps.core.locking import RetryExhausted

UNPROCESSABLE = 422
PROBLEM_TEMPLATE = "components/dialog_problem.html"
# Refusals that say the page is out of date (``edited``: the request changed after the page
# showed its ``seen_version``). No correction of the form helps, so a dialog gets 409.
STALE_CODES = frozenset({"edited"})


@dataclass(frozen=True)
class Dialog:
    """A form sent from a dialog, and how to show it again there with its errors.

    ``template`` names the form's fragment (``"app/page.html#partial"``, the one the dialog
    holds); ``context(form)`` gives everything that fragment needs, the bound form included.
    It is called only when the form is shown again.
    """

    form: Form
    template: str
    context: Callable[[Form], Mapping[str, Any]]


def varies(response: HttpResponse) -> HttpResponse:
    """Mark an answer that differs for htmx: no cache may give one to the other."""
    patch_vary_headers(response, ["HX-Request"])
    return response


def go(request: HttpRequest, to: str, *args: Any, **kwargs: Any) -> HttpResponse:
    """After a POST, go to ``to`` (a URL or a URL name with its arguments).

    A plain POST is redirected; an htmx one gets ``204`` and ``HX-Redirect``, so the whole
    page navigates and shows the flash the action left.
    """
    url = resolve_url(to, *args, **kwargs)
    if not is_htmx(request):
        return varies(redirect(url))
    response = HttpResponse(status=204)
    response["HX-Redirect"] = url
    return varies(response)


def form_again(request: HttpRequest, dialog: Dialog) -> HttpResponse:
    """The dialog's form with its errors: ``422``, swapped into the open dialog."""
    response = render(
        request, dialog.template, dict(dialog.context(dialog.form)), status=UNPROCESSABLE
    )
    return varies(response)


def _problem(request: HttpRequest, status: int) -> HttpResponse:
    """A refusal that no correction of the form can fix: the error page, or for htmx the
    dialog's message with the way out (reload the page)."""
    if is_htmx(request):
        response = render(request, PROBLEM_TEMPLATE, {"status": status}, status=status)
    else:
        response = render(request, f"{status}.html", status=status)
    return varies(response)


def idempotency_failure(request: HttpRequest, exc: IdempotencyError) -> HttpResponse:
    """400 for a tampered or foreign form key, 409 for an expired or reused one (plan §4.8)."""
    return _problem(request, exc.status_code)


def conflict(request: HttpRequest) -> HttpResponse:
    """The command kept conflicting with others (``RetryExhausted``): reload and retry."""
    return _problem(request, 409)


def _codes(exc: ValidationError) -> set[str | None]:
    errors = (
        [error for errors in exc.error_dict.values() for error in errors]
        if hasattr(exc, "error_dict")
        else exc.error_list
    )
    return {error.code for error in errors}


def _refused(
    request: HttpRequest, exc: ValidationError, to: str, dialog: Dialog | None
) -> HttpResponse:
    """A service refused the command: on the dialog's form (htmx; 409 when the page is out
    of date), or flashed."""
    if dialog is not None and is_htmx(request):
        if _codes(exc) & STALE_CODES:
            return _problem(request, 409)
        add_service_errors(dialog.form, exc)
        return form_again(request, dialog)
    messages.error(request, error_text(exc))
    return go(request, to)


def perform(
    request: HttpRequest,
    call: Callable[[], object],
    done: str,
    to: str,
    *,
    failed_to: str = "",
    dialog: Dialog | None = None,
) -> HttpResponse:
    """Run a service call: flash its outcome and go to ``to`` (``failed_to`` if it failed).

    With ``dialog``, an htmx POST that the service refuses gets the form again with the
    refusal on it (422) instead of the flash.
    """
    try:
        call()
    except ValidationError as exc:
        return _refused(request, exc, failed_to or to, dialog)
    except IdempotencyError as exc:
        return idempotency_failure(request, exc)
    except RetryExhausted:
        return conflict(request)
    messages.success(request, done)
    return go(request, to)


def invalid(
    request: HttpRequest, form: Form, to: str, *, dialog: Dialog | None = None
) -> HttpResponse:
    """A form that cannot be sent as it is: shown again in its dialog (htmx, 422), or its
    errors flashed and back to ``to``."""
    if dialog is not None and is_htmx(request):
        return form_again(request, dialog)
    messages.error(request, form_error_text(form) or _("Check the form."))
    return go(request, to)
