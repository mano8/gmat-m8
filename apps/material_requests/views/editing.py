"""The reviewer's edits of the lines of a submitted or approved request (M7b.4, Q32-Q36).

The dialog (GET) is one page for changing a line and one for adding a line: the material is
searched with a GET form and chosen with a link (``?material=``), so the page works without
JavaScript. The review page opens the same URL in a dialog (M8b.3): htmx asks for it and gets
only its ``line_dialog`` fragment. Saving, "save and approve" and removing are POST-only URLs
of their own that call the services of ``editing`` and redirect back to the review page with a
flash; sent from the dialog, they answer by the contract of ``apps.core.responses`` (204 and
``HX-Redirect``, or the form again with its errors, 422). The services
decide what an edit means (in place, or a replacement of a line that already has orders or
deliveries); the page only offers the tools.
"""

from dataclasses import dataclass
from typing import cast
from urllib.parse import urlencode
from uuid import UUID

from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils.cache import patch_vary_headers
from django.utils.translation import gettext as _
from django.views.decorators.http import require_safe

from apps.access.middleware import access_for
from apps.access.mixins import mutating, require_capability
from apps.access.roles import Capability
from apps.catalog import selectors as catalog_selectors
from apps.catalog.models import Material
from apps.core.http import is_htmx
from apps.core.idempotency import IdempotencyError, recorded_result
from apps.core.responses import Dialog, go, idempotency_failure, varies
from apps.material_requests import ledger, selectors, services
from apps.material_requests.forms import (
    LineChangeForm,
    LineRemoveForm,
    MaterialSearchForm,
    ReviewerLineAddForm,
    shown_quantity,
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


LINE_DIALOG = "line-dialog"
DIALOG_TEMPLATE = "material_requests/reviewer_line.html"
DIALOG_PARTIAL = f"{DIALOG_TEMPLATE}#line_dialog"


def _chosen_material(request: HttpRequest, value: str | None) -> Material | None:
    """The material picked on the dialog (``material``), if the user may view it."""
    try:
        pk = UUID(value or "")
    except ValueError:
        return None
    return catalog_selectors.materials(access_for(request)).filter(pk=pk).first()


def _in_dialog(request: HttpRequest) -> bool:
    """htmx asked for the content of the review page's dialog (``HX-Target``, compared, never
    printed). The change page's own swaps (opened as a link) target its panel, which has no
    dialog to close."""
    return is_htmx(request) and request.headers.get("HX-Target") == f"{LINE_DIALOG}-content"


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
        new_unit = chosen is not None and chosen.unit_id != line.unit_id
        initial |= {
            "free_text": line.free_text,
            "unit": line.unit_id,
            # Another unit: the quantity is given again, never carried over.
            "requested_qty": None if new_unit else shown_quantity(line),
        }
    return initial


@dataclass(frozen=True)
class Target:
    """What the dialog works on: a line to change, or the request a line is added to."""

    request: MaterialRequest
    line: MaterialRequestLine | None

    @property
    def page(self) -> str:
        """The dialog's own GET page (its search and material links stay on it)."""
        if self.line is not None:
            return reverse("requests:edit_line", args=[self.line.pk])
        return reverse("requests:new_line", args=[self.request.pk])

    @property
    def action(self) -> str:
        if self.line is not None:
            return reverse("requests:apply_edit", args=[self.line.pk])
        return reverse("requests:apply_add", args=[self.request.pk])


def _context(
    request: HttpRequest, target: Target, form: LineChangeForm, chosen: Material | None
) -> dict[str, object]:
    """What the dialog's fragment shows: the material search, the chosen material, the form."""
    ctx = access_for(request)
    found, line = target.request, target.line
    search = MaterialSearchForm(request.GET or None)
    query = search.cleaned_data["q"].strip() if search.is_valid() else ""
    return {
        "request_obj": found,
        "line": line,
        "form": form,
        "action": target.action,
        "page_url": target.page,
        "dialog_id": LINE_DIALOG if _in_dialog(request) else "",
        "back_url": _examine_url(found),
        "chosen": chosen,
        "search": search,
        "query": query,
        "materials": catalog_selectors.search(ctx, query) if query else [],
        "can_approve": found.status == Status.SUBMITTED and selectors.can_decide(ctx, found),
        "approved": found.status == Status.APPROVED,
        "described": chosen is None and (line is None or line.material_id is None),
    }


def _dialog(request: HttpRequest, target: Target) -> HttpResponse:
    """The change dialog of a line, or the dialog that adds a line: the whole page, or only
    the dialog's content for htmx (the review page's dialog loads it)."""
    ctx = access_for(request)
    chosen = _chosen_material(request, request.GET.get("material"))
    form_class = LineChangeForm if target.line else ReviewerLineAddForm
    form = form_class(
        ctx=ctx,
        line=target.line,
        actor_id=ctx.user_id,
        initial=_initial(target.request, target.line, chosen),
    )
    template = DIALOG_PARTIAL if is_htmx(request) else DIALOG_TEMPLATE
    response = varies(render(request, template, _context(request, target, form, chosen)))
    patch_vary_headers(response, ["HX-Target"])
    return response


def _sent_again(request: HttpRequest, target: Target, form: LineChangeForm) -> Dialog:
    """The dialog's form as it was sent, to show again with its errors (htmx, 422)."""
    chosen = _chosen_material(request, request.POST.get("material"))
    return Dialog(
        form,
        DIALOG_PARTIAL,
        lambda bound: _context(request, target, cast(LineChangeForm, bound), chosen),
    )


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
        return go(request, _examine_url(line))
    return _dialog(request, Target(line.request, line))


@mutating(EDIT)
def line_apply_edit(request: HttpRequest, line_id: UUID) -> HttpResponse:
    """Save the change (or save and approve it, in a submitted request)."""
    ctx = access_for(request)
    line = _on_line(request, line_id, EDIT)
    target = Target(line.request, line)
    dialog_page = _dialog_url("edit_line", line.pk, request.POST.get("material"))
    form = LineChangeForm(request.POST, ctx=ctx, line=line)
    again = _sent_again(request, target, form)
    if not form.is_valid():
        return _invalid(request, form, dialog_page, dialog=again)
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
        return go(request, _examine_url(line))
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
        failed_to=dialog_page,
        dialog=again,
    )


@require_safe
@require_capability(EDIT)
def line_new(request: HttpRequest, request_id: UUID) -> HttpResponse:
    """The dialog that adds a line to a submitted or approved request."""
    found = _on_request(request, request_id, EDIT)
    if not _editable(request, found, None):
        return go(request, _examine_url(found))
    return _dialog(request, Target(found, None))


@mutating(EDIT)
def line_apply_add(request: HttpRequest, request_id: UUID) -> HttpResponse:
    """Add the line (approved at once in an approved request; "save and approve" in a
    submitted one)."""
    ctx = access_for(request)
    found = _on_request(request, request_id, EDIT)
    dialog_page = _dialog_url("new_line", found.pk, request.POST.get("material"))
    form = ReviewerLineAddForm(request.POST, ctx=ctx)
    again = _sent_again(request, Target(found, None), form)
    if not form.is_valid():
        return _invalid(request, form, dialog_page, dialog=again)
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
        failed_to=dialog_page,
        dialog=again,
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
