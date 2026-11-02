"""The reviewer's edits of the lines of a submitted or approved request (M7b.4, Q32-Q36).

The dialog (GET) is one page for changing a line and one for adding a line: the material is
searched with a GET form and chosen with a link (``?material=``), so the page works without
JavaScript. Saving, "save and approve" and removing are POST-only URLs of their own that call
the services of ``editing`` and redirect back to the review page with a flash. The services
decide what an edit means (in place, or a replacement of a line that already has orders or
deliveries); the page only offers the tools.
"""

from urllib.parse import urlencode
from uuid import UUID

from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_safe

from apps.access.middleware import access_for
from apps.access.mixins import mutating, require_capability
from apps.access.roles import Capability
from apps.catalog import selectors as catalog_selectors
from apps.catalog.models import Material
from apps.core.idempotency import IdempotencyError, recorded_result
from apps.core.responses import idempotency_failure
from apps.material_requests import ledger, selectors, services
from apps.material_requests.forms import (
    LineChangeForm,
    LineRemoveForm,
    MaterialSearchForm,
    ReviewerLineAddForm,
)
from apps.material_requests.models import MaterialRequest, MaterialRequestLine
from apps.material_requests.services import LineEdit
from apps.material_requests.views.common import (
    _examine_url,
    _invalid,
    _on_line,
    _on_request,
    _perform,
)

EDIT = Capability.REQUEST_EDIT
Status = MaterialRequest.Status


def _chosen_material(request: HttpRequest) -> Material | None:
    """The material picked on the dialog (``?material=``), if the user may view it."""
    try:
        pk = UUID(request.GET.get("material", ""))
    except ValueError:
        return None
    return catalog_selectors.materials(access_for(request)).filter(pk=pk).first()


def _dialog_url(name: str, target_id: UUID, material: str | None) -> str:
    """The dialog's URL, keeping the material picked so far."""
    url = reverse(f"requests:{name}", args=[target_id])
    return f"{url}?{urlencode({'material': material})}" if material else url


def _initial(
    found: MaterialRequest, line: MaterialRequestLine | None, chosen: Material | None
) -> dict[str, object]:
    """The dialog form's values: the line as it is (nothing for a new line), the material
    picked so far and the version of the request the page shows."""
    initial: dict[str, object] = {"material": chosen, "seen_version": selectors.version(found)}
    if line is not None:
        initial |= {
            "free_text": line.free_text,
            "unit": line.unit_id,
            "requested_qty": line.requested_qty,
        }
    return initial


def _dialog(
    request: HttpRequest,
    found: MaterialRequest,
    line: MaterialRequestLine | None,
    *,
    action: str,
) -> HttpResponse:
    """The change dialog of ``line``, or the dialog that adds a line to ``found``."""
    ctx = access_for(request)
    chosen = _chosen_material(request)
    search = MaterialSearchForm(request.GET or None)
    query = search.cleaned_data["q"].strip() if search.is_valid() else ""
    form_class = LineChangeForm if line else ReviewerLineAddForm
    form = form_class(
        ctx=ctx, line=line, actor_id=ctx.user_id, initial=_initial(found, line, chosen)
    )
    context = {
        "request_obj": found,
        "line": line,
        "form": form,
        "action": action,
        "chosen": chosen,
        "search": search,
        "query": query,
        "materials": catalog_selectors.search(ctx, query) if query else [],
        "can_approve": found.status == Status.SUBMITTED and selectors.can_decide(ctx, found),
        "approved": found.status == Status.APPROVED,
        "described": chosen is None and (line is None or line.material_id is None),
    }
    return render(request, "material_requests/reviewer_line.html", context)


def _editable(
    request: HttpRequest, found: MaterialRequest, line: MaterialRequestLine | None
) -> bool:
    """Whether the dialog may open: the request is open to edits and, in an approved one,
    the line is live. Otherwise the reviewer is told why on the review page."""
    ctx = access_for(request)
    if not selectors.can_edit_lines(ctx, found) or (
        line is not None and found.status == Status.APPROVED and not ledger.is_live(line)
    ):
        messages.error(request, _("This line can no longer be edited."))
        return False
    return True


@require_safe
@require_capability(EDIT)
def line_edit(request: HttpRequest, line_id: UUID) -> HttpResponse:
    """The dialog that changes a line: material, description, unit or quantity, a reason."""
    line = _on_line(request, line_id, EDIT)
    if not _editable(request, line.request, line):
        return redirect(_examine_url(line))
    return _dialog(
        request, line.request, line, action=reverse("requests:apply_edit", args=[line.pk])
    )


@mutating(EDIT)
def line_apply_edit(request: HttpRequest, line_id: UUID) -> HttpResponse:
    """Save the change (or save and approve it, in a submitted request)."""
    ctx = access_for(request)
    line = _on_line(request, line_id, EDIT)
    dialog = _dialog_url("edit_line", line.pk, request.POST.get("material"))
    form = LineChangeForm(request.POST, ctx=ctx, line=line)
    if not form.is_valid():
        return _invalid(request, form, dialog)
    data = form.cleaned_data
    try:
        # A double tap: the first one changed the line, so what differs from it now is
        # another command. The key tells it was done.
        done = recorded_result(
            data["idempotency_key"], actor_id=ctx.principal.user_id, operation=form.operation
        )
    except IdempotencyError as exc:
        return idempotency_failure(request, exc)
    if done is not None:
        messages.success(request, _("Line %(line)s changed.") % {"line": line.line_no})
        return redirect(_examine_url(line))
    edit = LineEdit(
        reason=data["reason"],
        changes=form.changes(),
        decision=data["decision"],
        seen_version=data["seen_version"],
    )
    return _perform(
        request,
        lambda: services.edit_line(
            ctx.principal, line.pk, edit, idempotency_key=data["idempotency_key"]
        ),
        _("Line %(line)s changed.") % {"line": line.line_no},
        _examine_url(line),
        failed_to=dialog,
    )


@require_safe
@require_capability(EDIT)
def line_new(request: HttpRequest, request_id: UUID) -> HttpResponse:
    """The dialog that adds a line to a submitted or approved request."""
    found = _on_request(request, request_id, EDIT)
    if not _editable(request, found, None):
        return redirect(_examine_url(found))
    return _dialog(request, found, None, action=reverse("requests:apply_add", args=[found.pk]))


@mutating(EDIT)
def line_apply_add(request: HttpRequest, request_id: UUID) -> HttpResponse:
    """Add the line (approved at once in an approved request; "save and approve" in a
    submitted one)."""
    ctx = access_for(request)
    found = _on_request(request, request_id, EDIT)
    dialog = _dialog_url("new_line", found.pk, request.POST.get("material"))
    form = ReviewerLineAddForm(request.POST, ctx=ctx)
    if not form.is_valid():
        return _invalid(request, form, dialog)
    data = form.cleaned_data
    edit = LineEdit(
        reason=data["reason"],
        changes=form.changes(),
        decision=data["decision"],
        seen_version=data["seen_version"],
    )
    return _perform(
        request,
        lambda: services.add_line_as_reviewer(
            ctx.principal, found.pk, edit, idempotency_key=data["idempotency_key"]
        ),
        _("Line added."),
        _examine_url(found),
        failed_to=dialog,
    )


@mutating(EDIT)
def line_remove_reviewed(request: HttpRequest, line_id: UUID) -> HttpResponse:
    """Remove a line: rejected in a submitted request, closed short in an approved one. It
    stays on the request, with the reason."""
    ctx = access_for(request)
    line = _on_line(request, line_id, EDIT)
    form = LineRemoveForm(request.POST)
    if not form.is_valid():
        return _invalid(request, form, _examine_url(line))
    data = form.cleaned_data
    return _perform(
        request,
        lambda: services.remove_line_as_reviewer(
            ctx.principal,
            line.pk,
            reason=data["reason"],
            seen_version=data["seen_version"],
            idempotency_key=data["idempotency_key"],
        ),
        _("Line %(line)s removed.") % {"line": line.line_no},
        _examine_url(line),
    )
