"""Shared helpers of the request screens: scope-then-capability lookups, flash and redirect."""

from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.forms import Form
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.translation import gettext as _

from apps.access.middleware import access_for
from apps.access.mixins import require
from apps.access.roles import Capability
from apps.core.forms import error_text, form_error_text
from apps.core.locking import RetryExhausted
from apps.core.responses import conflict
from apps.material_requests import selectors
from apps.material_requests.models import MaterialRequest, MaterialRequestLine

VIEW, CREATE, WITHDRAW = (
    Capability.REQUEST_VIEW,
    Capability.REQUEST_CREATE,
    Capability.REQUEST_WITHDRAW,
)

REVIEW, SOURCE, CANCEL_ANY = (
    Capability.REQUEST_REVIEW,
    Capability.REQUEST_SOURCE,
    Capability.REQUEST_CANCEL_ANY,
)
Status = MaterialRequest.Status

LINES_TEMPLATE = "material_requests/draft_lines.html"


def _query(**params: str) -> str:
    """The filters, for the pager's links."""
    kept = {key: value for key, value in params.items() if value}
    return f"&{urlencode(kept)}" if kept else ""


def _own_draft(request: HttpRequest, request_id: UUID) -> MaterialRequest:
    """The request, if visible (404) and the user may create on it (403)."""
    return _on_request(request, request_id, CREATE)


def _own_line_request(request: HttpRequest, line_id: UUID) -> tuple[MaterialRequest, Any]:
    line = _on_line(request, line_id, CREATE)
    return line.request, line


# --- the reviewer: inbox, review page, material manager's tools (M4.4) -----------------
#
# The page is ``examine`` (GET); every action has its own POST-only URL and answers with a
# redirect back to the page, a flash message telling what happened or why not. The order of
# the checks is S7's again, with ``request.review`` / ``request.source`` / ``request.cancel_any``
# as the capability on the request and ``request.view`` as its read. The services decide.


def _on_request(request: HttpRequest, request_id: UUID, capability: str) -> MaterialRequest:
    found = selectors.request(access_for(request), request_id)
    require(request, capability, found, read=VIEW)
    return found


def _on_line(request: HttpRequest, line_id: UUID, capability: str) -> MaterialRequestLine:
    line = selectors.line(access_for(request), line_id)
    require(request, capability, line.request, read=VIEW)
    return line


def _examine_url(found: MaterialRequest | MaterialRequestLine) -> str:
    request_id = found.request_id if isinstance(found, MaterialRequestLine) else found.pk
    return reverse("requests:examine", args=[request_id])


def _perform(
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
    except RetryExhausted:
        return conflict(request)
    else:
        messages.success(request, done)
    return redirect(to)


def _invalid(request: HttpRequest, form: Form, to: str) -> HttpResponse:
    messages.error(request, form_error_text(form) or _("Check the form."))
    return redirect(to)
