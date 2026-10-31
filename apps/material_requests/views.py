"""The requester's screens (plan §11 M4.3): new request, materials, review, status, "My requests".

Flow: home → new request → project → materials → review → submit → status page. Reads go
through ``apps.material_requests.selectors``; writes through ``apps.material_requests
.services`` from POST-only views (``@mutating``), each on its own URL, with its GET page
on another. Every change of a draft answers an htmx request with the refreshed lines (the
page still works without JavaScript: the answer is then a redirect). The order of the
checks is S7's: the capability somewhere (403), the request in scope (404), the capability
on it (403). The services authorize again inside their transaction.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.forms import Form
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
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
from apps.material_requests import ledger, selectors, services
from apps.material_requests.forms import (
    DecisionForm,
    FreeLineForm,
    InboxFilterForm,
    LineAddForm,
    MaterialSearchForm,
    MyRequestsFilterForm,
    ProjectSearchForm,
    QuantityForm,
    ReasonForm,
    ReduceForm,
    RequiredReasonForm,
    ResolveForm,
    ReturnForm,
    ScheduleForm,
    SourcingForm,
    StartForm,
    SubmitForm,
)
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


@require_safe
@require_capability(REVIEW)
def inbox(request: HttpRequest) -> HttpResponse:
    ctx = access_for(request)
    filters = InboxFilterForm(request.GET or None, ctx=ctx)
    data = filters.cleaned_data if filters.is_valid() else {}
    status = data.get("status") or Status.SUBMITTED
    project = data.get("project")
    rows = selectors.inbox(
        ctx,
        status=status,
        priority=data.get("priority", ""),
        project_id=project.pk if project else None,
        needed_before=data.get("needed_before"),
    )
    page = Paginator(rows, selectors.PAGE_SIZE).get_page(request.GET.get("page"))
    template = (
        "material_requests/inbox.html#results"
        if is_htmx(request)
        else "material_requests/inbox.html"
    )
    context = {
        "filters": filters,
        "page_obj": page,
        "status": status,
        "extra_query": _query(
            status=request.GET.get("status", ""),
            priority=request.GET.get("priority", ""),
            project=request.GET.get("project", ""),
            needed_before=request.GET.get("needed_before", ""),
        ),
    }
    return render(request, template, context)


@dataclass
class ReviewRow:
    """A line of the review page and the forms its actions offer (None: not offered)."""

    entry: ledger.LineLedger
    decide: DecisionForm | None = None
    reduce: ReduceForm | None = None
    sourcing: SourcingForm | None = None
    close: RequiredReasonForm | None = None
    reopen: RequiredReasonForm | None = None
    link: bool = False


def _form_for[F: Form](form_class: type[F], line: MaterialRequestLine, **kwargs: Any) -> F:
    """A form whose ids are unique on the page (one per line)."""
    return form_class(auto_id=f"id_%s_{line.line_no}", **kwargs)


def _review_rows(
    ctx: Any, found: MaterialRequest, entries: list[ledger.LineLedger]
) -> list[ReviewRow]:
    deciding = found.status == Status.SUBMITTED and selectors.can_decide(ctx, found)
    approved = found.status == Status.APPROVED
    reviewing, sourcing = selectors.can_review(ctx, found), selectors.can_source(ctx, found)
    rows = []
    for entry in entries:
        line = entry.line
        row = ReviewRow(entry)
        live = approved and ledger.is_live(line)
        if deciding and (
            line.decision == line.Decision.PENDING or line.decided_by_id == ctx.user_id
        ):
            row.decide = _form_for(
                DecisionForm,
                line,
                initial={
                    "approved_qty": line.approved_qty or line.requested_qty,
                    "note": line.decision_note,
                },
            )
        if live and reviewing:
            row.reduce = _form_for(ReduceForm, line, initial={"approved_qty": entry.approved})
        if live and sourcing:
            initial = (
                {"sourcing": line.sourcing} if line.sourcing != line.Sourcing.UNDECIDED else {}
            )
            row.sourcing = _form_for(SourcingForm, line, initial=initial)
            row.close = _form_for(RequiredReasonForm, line)
            row.link = line.material_id is None
        if approved and sourcing and line.closed_short_source == line.ClosedShortSource.LINE:
            row.reopen = _form_for(RequiredReasonForm, line)
        rows.append(row)
    return rows


@require_safe
@require_capability(REVIEW)
def request_examine(request: HttpRequest, request_id: UUID) -> HttpResponse:
    """The review page: lines with their decision forms, the material manager's tools and
    the history of the request."""
    ctx = access_for(request)
    found = _on_request(request, request_id, REVIEW)
    entries = selectors.lines(ctx, [found])[found.pk]
    sourcing = selectors.can_source(ctx, found)
    context = {
        "request_obj": found,
        "rows": _review_rows(ctx, found, entries),
        "can_decide": selectors.can_decide(ctx, found),
        "can_source": sourcing,
        "can_cancel_any": selectors.can_cancel_any(ctx, found),
        "own_request": found.requested_by_id == ctx.user_id,
        "return_form": ReturnForm(),
        "schedule_form": ScheduleForm(initial=ScheduleForm.initial_for(found))
        if sourcing and found.status == Status.APPROVED
        else None,
        "reason_form": RequiredReasonForm(),
        "cancel_form": ReasonForm(),
        "history": selectors.history(ctx, found),
    }
    return render(request, "material_requests/examine.html", context)


@mutating(REVIEW)
def line_decide(request: HttpRequest, line_id: UUID) -> HttpResponse:
    """Approve (a quantity up to the requested one) or reject (with a reason) a line."""
    ctx = access_for(request)
    line = _on_line(request, line_id, REVIEW)
    to = _examine_url(line)
    form = DecisionForm(request.POST)
    if not form.is_valid():
        return _invalid(request, form, to)
    data = form.cleaned_data
    return _perform(
        request,
        lambda: services.decide_line(
            ctx.principal,
            line.pk,
            decision=data["decision"],
            approved_qty=data["approved_qty"],
            note=data["note"],
        ),
        _("Line %(line)s decided.") % {"line": line.line_no},
        to,
    )


@mutating(REVIEW)
def request_approve_all(request: HttpRequest, request_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    found = _on_request(request, request_id, REVIEW)
    return _perform(
        request,
        lambda: services.approve_all(ctx.principal, found.pk),
        _("Every pending line is approved."),
        _examine_url(found),
    )


@mutating(REVIEW)
def request_return(request: HttpRequest, request_id: UUID) -> HttpResponse:
    """Back to the requester with a note: every decision is cleared."""
    ctx = access_for(request)
    found = _on_request(request, request_id, REVIEW)
    form = ReturnForm(request.POST)
    if not form.is_valid():
        return _invalid(request, form, _examine_url(found))
    note = form.cleaned_data["note"]
    return _perform(
        request,
        lambda: services.return_to_draft(ctx.principal, found.pk, note=note),
        _("Request returned to its requester."),
        reverse("requests:inbox"),
        failed_to=_examine_url(found),
    )


@mutating(REVIEW)
def line_reduce(request: HttpRequest, line_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    line = _on_line(request, line_id, REVIEW)
    to = _examine_url(line)
    form = ReduceForm(request.POST)
    if not form.is_valid():
        return _invalid(request, form, to)
    data = form.cleaned_data
    return _perform(
        request,
        lambda: services.reduce_approved_qty(
            ctx.principal, line.pk, approved_qty=data["approved_qty"], reason=data["reason"]
        ),
        _("Approved quantity reduced."),
        to,
    )


@mutating(SOURCE)
def line_sourcing(request: HttpRequest, line_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    line = _on_line(request, line_id, SOURCE)
    to = _examine_url(line)
    form = SourcingForm(request.POST)
    if not form.is_valid():
        return _invalid(request, form, to)
    return _perform(
        request,
        lambda: services.set_sourcing(
            ctx.principal, line.pk, sourcing=form.cleaned_data["sourcing"]
        ),
        _("Sourcing saved."),
        to,
    )


@require_safe
@require_capability(SOURCE)
def line_link(request: HttpRequest, line_id: UUID) -> HttpResponse:
    """The dialog that links a non-catalog line to a catalog material of the same unit."""
    ctx = access_for(request)
    line = _on_line(request, line_id, SOURCE)
    if line.material_id is not None:
        messages.error(request, _("This line is already a catalog material."))
        return redirect(_examine_url(line))
    search = MaterialSearchForm(request.GET or None)
    query = search.cleaned_data["q"].strip() if search.is_valid() else ""
    context = {
        "line": line,
        "request_obj": line.request,
        "search": search,
        "query": query,
        "materials": selectors.link_candidates(ctx, line, query) if query else [],
    }
    return render(request, "material_requests/link.html", context)


@mutating(SOURCE)
def line_resolve(request: HttpRequest, line_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    line = _on_line(request, line_id, SOURCE)
    to = _examine_url(line)
    form = ResolveForm(request.POST, ctx=ctx)
    if not form.is_valid():
        return _invalid(request, form, reverse("requests:link", args=[line.pk]))
    return _perform(
        request,
        lambda: services.resolve_non_catalog_line(
            ctx.principal, line.pk, material_id=form.cleaned_data["material"].pk
        ),
        _("Line linked to the catalog."),
        to,
    )


def _reason_action(
    request: HttpRequest,
    target_id: UUID,
    service: Callable[..., object],
    done: str,
    *,
    on_line: bool,
) -> HttpResponse:
    """Close or reopen with a required reason, of a line or of the whole request."""
    ctx = access_for(request)
    target = (
        _on_line(request, target_id, SOURCE) if on_line else _on_request(request, target_id, SOURCE)
    )
    to = _examine_url(target)
    form = RequiredReasonForm(request.POST)
    if not form.is_valid():
        return _invalid(request, form, to)
    return _perform(
        request,
        lambda: service(ctx.principal, target.pk, reason=form.cleaned_data["reason"]),
        done,
        to,
    )


@mutating(SOURCE)
def line_close_short(request: HttpRequest, line_id: UUID) -> HttpResponse:
    return _reason_action(
        request, line_id, services.close_line_short, _("Line closed short."), on_line=True
    )


@mutating(SOURCE)
def line_reopen(request: HttpRequest, line_id: UUID) -> HttpResponse:
    return _reason_action(request, line_id, services.reopen_line, _("Line reopened."), on_line=True)


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
    return _reason_action(
        request, request_id, services.manual_close, _("Request closed."), on_line=False
    )


@mutating(SOURCE)
def request_reopen(request: HttpRequest, request_id: UUID) -> HttpResponse:
    return _reason_action(
        request, request_id, services.reopen, _("Request reopened."), on_line=False
    )


@mutating(CANCEL_ANY)
def request_cancel_any(request: HttpRequest, request_id: UUID) -> HttpResponse:
    """Cancel somebody else's request while nothing is allocated or delivered."""
    ctx = access_for(request)
    found = _on_request(request, request_id, CANCEL_ANY)
    form = ReasonForm(request.POST)
    reason = form.cleaned_data["reason"] if form.is_valid() else ""
    return _perform(
        request,
        lambda: services.cancel(ctx.principal, found.pk, reason=reason),
        _("Request cancelled."),
        _examine_url(found),
    )
