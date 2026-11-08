"""The reviewer's screens: inbox, review page, material manager's tools (plan §11 M4.4)."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from django.contrib import messages
from django.forms import Form
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_safe

from apps.access.middleware import access_for
from apps.access.mixins import mutating, require_capability
from apps.access.roles import Capability
from apps.attachments import selectors as attachment_selectors
from apps.core.dialogs import ActionDialog
from apps.core.listing import paginate, render_list
from apps.material_requests import ledger, selectors, services
from apps.material_requests.forms import (
    ApproveAllForm,
    AssignForm,
    DecisionForm,
    InboxFilterForm,
    LineRemoveForm,
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
)
from apps.material_requests.views.review_dialogs import (
    assign_dialog,
    cancel_dialog,
    close_dialog,
    close_line_dialog,
    line_form_ids,
    reduce_dialog,
    remove_line_dialog,
    reopen_dialog,
    reopen_line_dialog,
    request_form_ids,
    return_dialog,
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
        assigned_to_me=bool(data.get("assigned")),
    )
    listing = paginate(
        request,
        rows,
        default_per_page=selectors.DEFAULT_PER_PAGE,
        sorts=selectors.INBOX_SORTS,
        sort_labels=selectors.INBOX_SORT_LABELS,
    )
    context = {
        "filters": filters,
        "listing": listing,
        "status": status,
        "mine": selectors.assigned_ids(ctx, listing.rows),
    }
    return render_list(request, "material_requests/inbox.html", context)


@dataclass
class ReviewRow:
    """A line of the review page and the forms its actions offer (None: not offered)."""

    entry: ledger.LineLedger
    decide: DecisionForm | None = None
    reduce: ActionDialog | None = None  # the dialogs of the line's secondary actions (M8b.4)
    sourcing: SourcingForm | None = None
    close: ActionDialog | None = None
    reopen: ActionDialog | None = None
    link: bool = False
    edit: bool = False  # the way in to the change dialog (M7b.4)
    remove: ActionDialog | None = None
    history: selectors.LineHistory | None = None

    @property
    def menu_id(self) -> str:
        return f"line-menu-{self.entry.line.line_no}"

    @property
    def menu_items(self) -> list[dict[str, str]]:
        """The line's secondary actions, for its menu (the dialogs, in the page's order)."""
        return [d.menu_item() for d in (self.reduce, self.close, self.remove, self.reopen) if d]


def _form_for[F: Form](
    form_class: type[F], line: MaterialRequestLine, name: str = "", **kwargs: Any
) -> F:
    """A form whose ids are unique on the page (one per line, and per dialog of the line)."""
    return form_class(auto_id=line_form_ids(line, name), **kwargs)


@dataclass(frozen=True)
class RowAccess:
    """What the user may offer on the lines of one request, and in which state."""

    user_id: Any
    deciding: bool  # a submitted request this user may decide
    approved: bool  # an approved request
    reducing: bool
    sourcing: bool
    editing: bool = False  # ``request.edit`` on a submitted or approved request
    version: int = 0  # ``selectors.version``: sent back with every decision and edit


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
            "seen_version": access.version,
        },
    )


def _live_tools(row: ReviewRow, access: RowAccess) -> None:
    """The reduction, sourcing and closing forms of a live line of an approved request."""
    line = row.entry.line
    if access.reducing:
        form = _form_for(ReduceForm, line, "reduce", initial={"approved_qty": row.entry.approved})
        row.reduce = reduce_dialog(line, form, row.entry.floor)
    if access.sourcing:
        initial = {"sourcing": line.sourcing} if line.sourcing != line.Sourcing.UNDECIDED else {}
        row.sourcing = _form_for(SourcingForm, line, initial=initial)
        row.close = close_line_dialog(line, _form_for(RequiredReasonForm, line, "close"))
        row.link = line.material_id is None


def _edit_tools(row: ReviewRow, access: RowAccess) -> None:
    """The change dialog and the removal of a line a reviewer may edit: any line of a
    submitted request (a rejected one is already removed), a live line of an approved one
    that is not fully delivered (the services check the floors)."""
    line = row.entry.line
    if access.approved:
        editable = ledger.is_live(line)
        removable = editable and not row.entry.fully_delivered
    else:
        editable, removable = True, line.decision != line.Decision.REJECTED
    row.edit = editable
    if removable:
        form = _form_for(
            LineRemoveForm,
            line,
            "remove",
            actor_id=access.user_id,
            initial={"seen_version": access.version},
        )
        status = Status.APPROVED if access.approved else Status.SUBMITTED
        row.remove = remove_line_dialog(line, form, status)


def _review_row(
    access: RowAccess, entry: ledger.LineLedger, history: selectors.LineHistory | None
) -> ReviewRow:
    line = entry.line
    row = ReviewRow(entry, decide=_decide_form(access, line), history=history)
    if access.approved and ledger.is_live(line):
        _live_tools(row, access)
    if (
        access.approved
        and access.sourcing
        and line.closed_short_source == line.ClosedShortSource.LINE
    ):
        row.reopen = reopen_line_dialog(line, _form_for(RequiredReasonForm, line, "reopen"))
    if access.editing:
        _edit_tools(row, access)
    return row


def _review_rows(
    ctx: Any, found: MaterialRequest, entries: list[ledger.LineLedger], access: RowAccess
) -> list[ReviewRow]:
    history = {item.line.pk: item for item in selectors.line_history(ctx, found)}
    return [_review_row(access, entry, history.get(entry.line.pk)) for entry in entries]


def _request_dialogs(
    ctx: Any, found: MaterialRequest, reviewers: list[Any], *, sourcing: bool
) -> list[ActionDialog]:
    """The request's secondary actions as dialogs, in the order of the page's menu: send to
    another reviewer, return for changes, close or reopen, cancel (each only when offered)."""
    dialogs: list[ActionDialog] = []
    open_for_review = found.status in (Status.SUBMITTED, Status.APPROVED)
    if open_for_review and reviewers:
        assign = AssignForm(reviewers=reviewers, auto_id=request_form_ids("assign"))
        dialogs.append(assign_dialog(found, assign))
    if found.status == Status.SUBMITTED and selectors.can_decide(ctx, found):
        dialogs.append(return_dialog(found, ReturnForm(auto_id=request_form_ids("return"))))
    if sourcing and found.status == Status.APPROVED:
        warning = selectors.ships_to_own_worksite(ctx, found)
        form = RequiredReasonForm(auto_id=request_form_ids("close"))
        dialogs.append(close_dialog(found, form, worksite_warning=warning))
    if sourcing and found.status == Status.CLOSED:
        dialogs.append(reopen_dialog(found, RequiredReasonForm(auto_id=request_form_ids("reopen"))))
    if selectors.can_cancel_any(ctx, found):
        dialogs.append(cancel_dialog(found, ReasonForm(auto_id=request_form_ids("cancel"))))
    return dialogs


def _dialog_context(dialogs: list[ActionDialog]) -> dict[str, Any]:
    """What the page needs of its dialogs: all of them, the menu's items (the reviewer is sent
    from a button beside the assignee, not from the menu) and the assign form."""
    assign = next((d for d in dialogs if d.id == "assign-dialog"), None)
    return {
        "dialogs": dialogs,
        "assign_form": assign.form if assign else None,
        "menu_items": [d.menu_item() for d in dialogs if d is not assign],
    }


@require_safe
@require_capability(REVIEW)
def request_examine(request: HttpRequest, request_id: UUID) -> HttpResponse:
    """The review page: lines with their decision forms, the material manager's tools and
    the history of the request."""
    ctx = access_for(request)
    found = _on_request(request, request_id, REVIEW)
    entries = selectors.lines(ctx, [found])[found.pk]
    sourcing = selectors.can_source(ctx, found)
    reviewers = _assignable(ctx, found) if selectors.can_assign(ctx, found) else []
    access = RowAccess(
        user_id=ctx.user_id,
        deciding=found.status == Status.SUBMITTED and selectors.can_decide(ctx, found),
        approved=found.status == Status.APPROVED,
        reducing=selectors.can_decide(ctx, found),
        sourcing=sourcing,
        editing=selectors.can_edit_lines(ctx, found),
        version=selectors.version(found),
    )
    dialogs = _request_dialogs(ctx, found, reviewers, sourcing=sourcing)
    rows = _review_rows(ctx, found, entries, access)
    context = {
        "request_obj": found,
        "rows": rows,
        "assignee": selectors.assignee(found),
        **_dialog_context(dialogs),
        "pending_count": sum(
            1 for row in rows if row.entry.line.decision == MaterialRequestLine.Decision.PENDING
        ),
        "can_edit_lines": access.editing,
        "approve_all_form": ApproveAllForm(initial={"seen_version": access.version}),
        "can_decide": selectors.can_decide(ctx, found),
        "can_source": sourcing,
        "own_request": found.requested_by_id == ctx.user_id,
        "schedule_form": ScheduleForm(initial=ScheduleForm.initial_for(found))
        if sourcing and found.status == Status.APPROVED
        else None,
        "history": selectors.history(ctx, found),
        "attachments": attachment_selectors.panel(ctx, found, back="examine"),
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
            seen_version=data["seen_version"],
        ),
        _("Line %(line)s decided.") % {"line": line.line_no},
        to,
    )


@mutating(REVIEW)
def request_approve_all(request: HttpRequest, request_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    found = _on_request(request, request_id, REVIEW)
    form = ApproveAllForm(request.POST)
    if not form.is_valid():
        return _invalid(request, form, _examine_url(found))
    seen = form.cleaned_data["seen_version"]
    return _perform(
        request,
        lambda: services.approve_all(ctx.principal, found.pk, seen_version=seen),
        _("Every pending line is approved."),
        _examine_url(found),
    )


def _assignable(ctx: Any, found: MaterialRequest) -> list[Any]:
    """The reviewers the request can be sent to: not the one it is already with (assigning
    them again would change nothing and drop the note)."""
    return [
        user
        for user in selectors.eligible_reviewers(ctx, found)
        if user.pk != found.assigned_reviewer_id
    ]


@mutating(REVIEW)
def request_assign(request: HttpRequest, request_id: UUID) -> HttpResponse:
    """Send the request to another reviewer, with a note (the choices come from the
    selector, the service judges the assignee again)."""
    ctx = access_for(request)
    found = _on_request(request, request_id, REVIEW)
    form = AssignForm(
        request.POST, reviewers=_assignable(ctx, found), auto_id=request_form_ids("assign")
    )
    dialog = assign_dialog(found, form).dialog()
    if not form.is_valid():
        return _invalid(request, form, _examine_url(found), dialog=dialog)
    return _perform(
        request,
        lambda: services.assign_reviewer(
            ctx.principal,
            found.pk,
            reviewer_id=form.reviewer_id(),
            note=form.cleaned_data["note"],
        ),
        _("Request sent to the reviewer."),
        _examine_url(found),
        dialog=dialog,
    )


@mutating(REVIEW)
def request_return(request: HttpRequest, request_id: UUID) -> HttpResponse:
    """Back to the requester with a note: every decision is cleared."""
    ctx = access_for(request)
    found = _on_request(request, request_id, REVIEW)
    form = ReturnForm(request.POST, auto_id=request_form_ids("return"))
    dialog = return_dialog(found, form).dialog()
    if not form.is_valid():
        return _invalid(request, form, _examine_url(found), dialog=dialog)
    note = form.cleaned_data["note"]
    return _perform(
        request,
        lambda: services.return_to_draft(ctx.principal, found.pk, note=note),
        _("Request returned to its requester."),
        reverse("requests:inbox"),
        failed_to=_examine_url(found),
        dialog=dialog,
    )


@mutating(REVIEW)
def line_reduce(request: HttpRequest, line_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    line = _on_line(request, line_id, REVIEW)
    to = _examine_url(line)
    form = _form_for(ReduceForm, line, "reduce", data=request.POST)
    entry = next(
        e for e in selectors.lines(ctx, [line.request])[line.request_id] if e.line.pk == line.pk
    )
    dialog = reduce_dialog(line, form, entry.floor).dialog()
    if not form.is_valid():
        return _invalid(request, form, to, dialog=dialog)
    data = form.cleaned_data
    return _perform(
        request,
        lambda: services.reduce_approved_qty(
            ctx.principal, line.pk, approved_qty=data["approved_qty"], reason=data["reason"]
        ),
        _("Approved quantity reduced."),
        to,
        dialog=dialog,
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


@mutating(CANCEL_ANY)
def request_cancel_any(request: HttpRequest, request_id: UUID) -> HttpResponse:
    """Cancel somebody else's request while nothing is allocated or delivered."""
    ctx = access_for(request)
    found = _on_request(request, request_id, CANCEL_ANY)
    form = ReasonForm(request.POST, auto_id=request_form_ids("cancel"))
    reason = form.cleaned_data["reason"] if form.is_valid() else ""
    return _perform(
        request,
        lambda: services.cancel(ctx.principal, found.pk, reason=reason),
        _("Request cancelled."),
        _examine_url(found),
        dialog=cancel_dialog(found, form).dialog(),
    )
