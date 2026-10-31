"""The requester's screens (plan §11 M4.3)."""

from typing import Any
from uuid import UUID

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.forms import Form
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_safe

from apps.access.middleware import access_for
from apps.access.mixins import mutating, require, require_capability
from apps.access.roles import Capability
from apps.catalog import selectors as catalog_selectors
from apps.core.forms import add_service_errors, error_text, form_error_text
from apps.core.http import is_htmx
from apps.core.idempotency import IdempotencyError
from apps.core.locking import RetryExhausted
from apps.core.responses import conflict, idempotency_failure
from apps.material_requests import selectors, services
from apps.material_requests.forms import (
    FreeLineForm,
    LineAddForm,
    MaterialSearchForm,
    MyRequestsFilterForm,
    ProjectSearchForm,
    QuantityForm,
    ReasonForm,
    StartForm,
    SubmitForm,
)
from apps.material_requests.models import MaterialRequest
from apps.material_requests.views.common import (
    _own_draft,
    _own_line_request,
    _query,
)

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


# --- My requests ------------------------------------------------------------------------


@require_safe
@require_capability(VIEW)
def request_list(request: HttpRequest) -> HttpResponse:
    ctx = access_for(request)
    filters = MyRequestsFilterForm(request.GET or None)
    status = filters.cleaned_data["status"] if filters.is_valid() else ""
    page = Paginator(selectors.my_requests(ctx, status=status), selectors.PAGE_SIZE).get_page(
        request.GET.get("page")
    )
    grouped = selectors.lines(ctx, list(page.object_list))
    context = {
        "filters": filters,
        "status": status,
        "statuses": filters.fields["status"].choices,  # type: ignore[attr-defined]
        "page_obj": page,
        "rows": [(item, grouped[item.pk]) for item in page.object_list],
        "extra_query": _query(status=status),
        "can_create": ctx.has(CREATE),
    }
    return render(request, "material_requests/list.html", context)


# --- new request: choose the project ------------------------------------------------------


@require_safe
@require_capability(CREATE)
def request_new(request: HttpRequest) -> HttpResponse:
    ctx = access_for(request)
    search = ProjectSearchForm(request.GET or None)
    q = search.cleaned_data["q"] if search.is_valid() else ""
    page = Paginator(selectors.creatable_projects(ctx, q=q), selectors.PROJECT_PAGE_SIZE).get_page(
        request.GET.get("page")
    )
    # One key per button: tapping another project is another command (plan §4.8).
    entries = [
        (project, StartForm(ctx=ctx, actor_id=ctx.user_id, initial={"project": project.pk}))
        for project in page.object_list
    ]
    template = (
        "material_requests/new.html#results" if is_htmx(request) else "material_requests/new.html"
    )
    context = {
        "filters": search,
        "page_obj": page,
        "entries": entries,
        "extra_query": _query(q=q),
    }
    return render(request, template, context)


@mutating(CREATE)
def request_start(request: HttpRequest) -> HttpResponse:
    """Tapping a project creates the draft (a double tap replays the same key)."""
    ctx = access_for(request)
    form = StartForm(request.POST, ctx=ctx)
    if not form.is_valid():
        messages.error(request, form_error_text(form) or _("Choose a project."))
        return redirect("requests:new")
    try:
        draft = services.create_draft(
            ctx.principal,
            project_id=form.cleaned_data["project"].pk,
            idempotency_key=form.cleaned_data["idempotency_key"],
        )
    except ValidationError as exc:
        messages.error(request, error_text(exc))
        return redirect("requests:new")
    except IdempotencyError as exc:
        return idempotency_failure(request, exc)
    except RetryExhausted:
        return conflict(request)
    return redirect("requests:detail", request_id=draft.pk)


# --- the request: materials of a draft, or the status page -------------------------------


def _draft_context(
    request: HttpRequest,
    found: MaterialRequest,
    *,
    error: str = "",
    free_form: FreeLineForm | None = None,
) -> dict[str, Any]:
    """What the lines editor and the picker show for a draft."""
    ctx = access_for(request)
    editable = selectors.can_edit(ctx, found)
    return {
        "request_obj": found,
        "lines": selectors.lines(ctx, [found])[found.pk],
        "error": error,
        "editable": editable,
        "free_form": free_form or FreeLineForm(ctx=ctx, actor_id=ctx.user_id),
        "search": MaterialSearchForm(),
        "materials": selectors.recent_materials(ctx) if editable else [],
        "query": "",
    }


@require_safe
@require_capability(VIEW)
def request_detail(request: HttpRequest, request_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    found = selectors.request(ctx, request_id)
    context = _draft_context(request, found)
    context["withdrawable"] = selectors.can_withdraw(ctx, found)
    context["reviewable"] = found.status != Status.DRAFT and selectors.can_review(ctx, found)
    return render(request, "material_requests/detail.html", context)


@require_safe
@require_capability(CREATE)
def request_materials(request: HttpRequest, request_id: UUID) -> HttpResponse:
    """The picker's fragment: the best matches for ``?q=``, or the recent materials."""
    ctx = access_for(request)
    found = _own_draft(request, request_id)
    search = MaterialSearchForm(request.GET or None)
    query = search.cleaned_data["q"].strip() if search.is_valid() else ""
    context = {
        "request_obj": found,
        "query": query,
        "materials": catalog_selectors.search(ctx, query)
        if query
        else selectors.recent_materials(ctx),
    }
    return render(request, "material_requests/material_picker.html", context)


@require_safe
@require_capability(CREATE)
def request_review(request: HttpRequest, request_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    found = _own_draft(request, request_id)
    form = SubmitForm(initial=SubmitForm.initial_for(found), actor_id=ctx.user_id)
    return _review(request, found, form)


def _review(request: HttpRequest, found: MaterialRequest, form: Form) -> HttpResponse:
    ctx = access_for(request)
    context = {
        "request_obj": found,
        "form": form,
        "lines": selectors.lines(ctx, [found])[found.pk],
        "editable": selectors.can_edit(ctx, found),
    }
    return render(request, "material_requests/review.html", context)


# --- changes to a draft ------------------------------------------------------------------


def _lines_answer(
    request: HttpRequest,
    found: MaterialRequest,
    *,
    error: str = "",
    free_form: FreeLineForm | None = None,
) -> HttpResponse:
    """htmx: the refreshed lines. Otherwise back to the request (an error is flashed)."""
    if is_htmx(request):
        return render(
            request,
            LINES_TEMPLATE,
            _draft_context(request, found, error=error, free_form=free_form),
        )
    if error:
        messages.error(request, error)
    return redirect("requests:detail", request_id=found.pk)


@mutating(CREATE)
def line_add(request: HttpRequest, request_id: UUID) -> HttpResponse:
    """Tapping a material adds one of it; the same material again adds up on its line."""
    ctx = access_for(request)
    found = _own_draft(request, request_id)
    form = LineAddForm(request.POST, ctx=ctx)
    if not form.is_valid():
        return _lines_answer(request, found, error=form_error_text(form))
    material, quantity = form.cleaned_data["material"], form.quantity()
    try:
        existing = selectors.line_for_material(ctx, found, material.pk)
        if existing is None:
            services.add_line(
                ctx.principal, found.pk, material_id=material.pk, requested_qty=quantity
            )
        else:
            services.update_line(
                ctx.principal, existing.pk, requested_qty=existing.requested_qty + quantity
            )
    except ValidationError as exc:
        return _lines_answer(request, found, error=error_text(exc))
    except RetryExhausted:
        return conflict(request)
    return _lines_answer(request, found)


@mutating(CREATE)
def line_add_free(request: HttpRequest, request_id: UUID) -> HttpResponse:
    """A material that is not in the catalog: described, with its unit and quantity."""
    ctx = access_for(request)
    found = _own_draft(request, request_id)
    form = FreeLineForm(request.POST, ctx=ctx)
    if not form.is_valid():
        return _lines_answer(request, found, error=form_error_text(form), free_form=form)
    data = form.cleaned_data
    try:
        services.add_line(
            ctx.principal,
            found.pk,
            free_text=data["free_text"],
            unit_id=data["unit"].pk,
            requested_qty=data["requested_qty"],
            idempotency_key=data["idempotency_key"],
        )
    except ValidationError as exc:
        add_service_errors(form, exc)
        return _lines_answer(request, found, error=form_error_text(form), free_form=form)
    except IdempotencyError as exc:
        return idempotency_failure(request, exc)
    except RetryExhausted:
        return conflict(request)
    return _lines_answer(request, found)


@mutating(CREATE)
def line_update(request: HttpRequest, line_id: UUID) -> HttpResponse:
    """The stepper: minus and plus change the quantity by one, the field sets it."""
    ctx = access_for(request)
    found, line = _own_line_request(request, line_id)
    form = QuantityForm(request.POST)
    if not form.is_valid():
        return _lines_answer(request, found, error=form_error_text(form))
    quantity = form.new_quantity()
    if quantity <= 0:
        return _lines_answer(
            request, found, error=_("The quantity must be positive; remove the line instead.")
        )
    try:
        services.update_line(ctx.principal, line.pk, requested_qty=quantity)
    except ValidationError as exc:
        return _lines_answer(request, found, error=error_text(exc))
    except RetryExhausted:
        return conflict(request)
    return _lines_answer(request, found)


@mutating(CREATE)
def line_remove(request: HttpRequest, line_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    found, line = _own_line_request(request, line_id)
    try:
        services.remove_line(ctx.principal, line.pk)
    except ValidationError as exc:
        return _lines_answer(request, found, error=error_text(exc))
    except RetryExhausted:
        return conflict(request)
    return _lines_answer(request, found)


# --- transitions -------------------------------------------------------------------------


@mutating(CREATE)
def request_submit(request: HttpRequest, request_id: UUID) -> HttpResponse:
    """Review step and submit in one transaction; a double tap replays the same key."""
    ctx = access_for(request)
    found = _own_draft(request, request_id)
    form = SubmitForm(request.POST, actor_id=ctx.user_id)
    if form.is_valid():
        try:
            submitted = services.submit(
                ctx.principal,
                found.pk,
                idempotency_key=form.cleaned_data["idempotency_key"],
                **form.header(),
            )
        except ValidationError as exc:
            add_service_errors(form, exc)
        except IdempotencyError as exc:
            return idempotency_failure(request, exc)
        except RetryExhausted:
            return conflict(request)
        else:
            messages.success(
                request, _("Request %(number)s submitted.") % {"number": submitted.number}
            )
            return redirect("requests:detail", request_id=submitted.pk)
    return _review(request, found, form)


@mutating(WITHDRAW)
def request_withdraw(request: HttpRequest, request_id: UUID) -> HttpResponse:
    """Back to draft while no reviewer has decided a line."""
    ctx = access_for(request)
    found = selectors.request(ctx, request_id)
    require(request, WITHDRAW, found, read=VIEW)
    try:
        services.withdraw(ctx.principal, found.pk)
    except ValidationError as exc:
        messages.error(request, error_text(exc))
    except RetryExhausted:
        return conflict(request)
    else:
        messages.success(request, _("Request withdrawn; it is a draft again."))
    return redirect("requests:detail", request_id=found.pk)


@mutating(CREATE)
def request_cancel(request: HttpRequest, request_id: UUID) -> HttpResponse:
    """Discard a draft (or cancel an own request no reviewer has decided yet)."""
    ctx = access_for(request)
    found = _own_draft(request, request_id)
    form = ReasonForm(request.POST)
    reason = form.cleaned_data["reason"] if form.is_valid() else ""
    try:
        services.cancel(ctx.principal, found.pk, reason=reason)
    except ValidationError as exc:
        messages.error(request, error_text(exc))
        return redirect("requests:detail", request_id=found.pk)
    except RetryExhausted:
        return conflict(request)
    messages.success(request, _("Request discarded."))
    return redirect("requests:list")
