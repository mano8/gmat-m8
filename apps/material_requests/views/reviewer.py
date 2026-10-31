"""The reviewer's screens: inbox, review page, material manager's tools (plan §11 M4.4)."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from django.contrib import messages
from django.core.paginator import Paginator
from django.forms import Form
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_safe

from apps.access.middleware import access_for
from apps.access.mixins import mutating, require_capability
from apps.access.roles import Capability
from apps.core.http import is_htmx
from apps.material_requests import ledger, selectors, services
from apps.material_requests.forms import (
    DecisionForm,
    InboxFilterForm,
    MaterialSearchForm,
    ReasonForm,
    ReduceForm,
    RequiredReasonForm,
    ResolveForm,
    ReturnForm,
    ScheduleForm,
    SourcingForm,
)
from apps.material_requests.models import MaterialRequest, MaterialRequestLine
from apps.material_requests.views.common import (
    _examine_url,
    _invalid,
    _on_line,
    _on_request,
    _perform,
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


@dataclass(frozen=True)
class RowAccess:
    """What the user may offer on the lines of one request, and in which state."""

    user_id: Any
    deciding: bool  # a submitted request this user may decide
    approved: bool  # an approved request
    reviewing: bool
    sourcing: bool


def _decide_form(access: RowAccess, line: MaterialRequestLine) -> DecisionForm | None:
    """Pending lines, and the lines this reviewer decided themselves."""
    if not access.deciding:
        return None
    if line.decision != line.Decision.PENDING and line.decided_by_id != access.user_id:
        return None
    return _form_for(
        DecisionForm,
        line,
        initial={
            "approved_qty": line.approved_qty or line.requested_qty,
            "note": line.decision_note,
        },
    )


def _live_tools(row: ReviewRow, access: RowAccess) -> None:
    """The reduction, sourcing and closing forms of a live line of an approved request."""
    line = row.entry.line
    row.reduce = (
        _form_for(ReduceForm, line, initial={"approved_qty": row.entry.approved})
        if access.reviewing
        else None
    )
    if access.sourcing:
        initial = {"sourcing": line.sourcing} if line.sourcing != line.Sourcing.UNDECIDED else {}
        row.sourcing = _form_for(SourcingForm, line, initial=initial)
        row.close = _form_for(RequiredReasonForm, line)
        row.link = line.material_id is None


def _review_row(access: RowAccess, entry: ledger.LineLedger) -> ReviewRow:
    line = entry.line
    row = ReviewRow(entry, decide=_decide_form(access, line))
    if access.approved and ledger.is_live(line):
        _live_tools(row, access)
    if (
        access.approved
        and access.sourcing
        and line.closed_short_source == line.ClosedShortSource.LINE
    ):
        row.reopen = _form_for(RequiredReasonForm, line)
    return row


def _review_rows(
    ctx: Any, found: MaterialRequest, entries: list[ledger.LineLedger]
) -> list[ReviewRow]:
    access = RowAccess(
        user_id=ctx.user_id,
        deciding=found.status == Status.SUBMITTED and selectors.can_decide(ctx, found),
        approved=found.status == Status.APPROVED,
        reviewing=selectors.can_review(ctx, found),
        sourcing=selectors.can_source(ctx, found),
    )
    return [_review_row(access, entry) for entry in entries]


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
