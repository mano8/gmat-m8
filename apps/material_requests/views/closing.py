"""The material manager's closures on the review page (plan §4.5, M4.4): close a line short or
reopen it, close or reopen the whole request, each with a required reason, and the schedule of
an approved request. Split from ``reviewer`` (file size)."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from django.forms import Form
from django.http import HttpRequest, HttpResponse
from django.utils.translation import gettext as _

from apps.access.middleware import access_for
from apps.access.mixins import mutating
from apps.access.roles import Capability
from apps.core.dialogs import ActionDialog
from apps.material_requests import selectors, services
from apps.material_requests.forms import RequiredReasonForm, ScheduleForm
from apps.material_requests.models import MaterialRequest
from apps.material_requests.views.common import (
    _examine_url,
    _invalid,
    _on_line,
    _on_request,
    _perform,
)
from apps.material_requests.views.review_dialogs import (
    close_dialog,
    close_line_dialog,
    line_form_ids,
    reopen_dialog,
    reopen_line_dialog,
    request_form_ids,
)

SOURCE = Capability.REQUEST_SOURCE


@dataclass(frozen=True)
class ReasonAction:
    """Close or reopen with a required reason: the service, what is flashed, and the dialog the
    form is shown in again (``dialog(ctx, target, form)``) when it is invalid or refused."""

    service: Callable[..., object]
    done: str
    dialog: Callable[[Any, Any, Form], ActionDialog]
    ids: Callable[[Any], str]
    on_line: bool = False


def _reason_action(request: HttpRequest, target_id: UUID, action: ReasonAction) -> HttpResponse:
    """Close or reopen with a required reason, of a line or of the whole request."""
    ctx = access_for(request)
    target = (
        _on_line(request, target_id, SOURCE)
        if action.on_line
        else _on_request(request, target_id, SOURCE)
    )
    to = _examine_url(target)
    form = RequiredReasonForm(request.POST, auto_id=action.ids(target))
    dialog = action.dialog(ctx, target, form).dialog()
    if not form.is_valid():
        return _invalid(request, form, to, dialog=dialog)
    return _perform(
        request,
        lambda: action.service(ctx.principal, target.pk, reason=form.cleaned_data["reason"]),
        action.done,
        to,
        dialog=dialog,
    )


def _close_request_dialog(ctx: Any, found: MaterialRequest, form: Form) -> ActionDialog:
    warning = selectors.ships_to_own_worksite(ctx, found)
    return close_dialog(found, form, worksite_warning=warning)


def _close_line() -> ReasonAction:
    return ReasonAction(
        services.close_line_short,
        _("Line closed short."),
        lambda ctx, line, form: close_line_dialog(line, form),
        lambda line: line_form_ids(line, "close"),
        on_line=True,
    )


def _reopen_line() -> ReasonAction:
    return ReasonAction(
        services.reopen_line,
        _("Line reopened."),
        lambda ctx, line, form: reopen_line_dialog(line, form),
        lambda line: line_form_ids(line, "reopen"),
        on_line=True,
    )


def _close_request() -> ReasonAction:
    return ReasonAction(
        services.manual_close,
        _("Request closed."),
        _close_request_dialog,
        lambda found: request_form_ids("close"),
    )


def _reopen_request() -> ReasonAction:
    return ReasonAction(
        services.reopen,
        _("Request reopened."),
        lambda ctx, found, form: reopen_dialog(found, form),
        lambda found: request_form_ids("reopen"),
    )


@mutating(SOURCE)
def line_close_short(request: HttpRequest, line_id: UUID) -> HttpResponse:
    return _reason_action(request, line_id, _close_line())


@mutating(SOURCE)
def line_reopen(request: HttpRequest, line_id: UUID) -> HttpResponse:
    return _reason_action(request, line_id, _reopen_line())


@mutating(SOURCE)
def request_schedule(request: HttpRequest, request_id: UUID) -> HttpResponse:
    """Priority and needed-by date of an approved request."""
    ctx = access_for(request)
    found = _on_request(request, request_id, SOURCE)
    to = _examine_url(found)
    form = ScheduleForm(request.POST)
    if not form.is_valid():
        return _invalid(request, form, to)
    data = form.cleaned_data
    return _perform(
        request,
        lambda: services.change_schedule(
            ctx.principal, found.pk, priority=data["priority"], needed_by=data["needed_by"]
        ),
        _("Schedule saved."),
        to,
    )


@mutating(SOURCE)
def request_close(request: HttpRequest, request_id: UUID) -> HttpResponse:
    return _reason_action(request, request_id, _close_request())


@mutating(SOURCE)
def request_reopen(request: HttpRequest, request_id: UUID) -> HttpResponse:
    return _reason_action(request, request_id, _reopen_request())
