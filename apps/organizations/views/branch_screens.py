"""Branches (M10.2): list, page, create, edit (with the default warehouse), the managing
branch, self-approval, deactivate and reactivate.

Every page and action needs ``org.manage`` (403 otherwise; a branch out of scope is 404).
Reads go through the selectors; writes through ``apps.organizations.services`` from POST-only
views, each on its own URL, answering by the dialog contract of ``apps.core.responses``. The
managing branch and the activity change who reaches what: their danger dialogs preview it
(``preview_branch_manager`` refreshes the preview as a managing branch is chosen) and carry
the generation that preview was read at; the service decides.
"""

from contextlib import suppress
from uuid import UUID

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_safe

from apps.access.context import AccessContext
from apps.access.middleware import access_for
from apps.access.mixins import mutating, require_capability
from apps.core.dialogs import lazy
from apps.core.listing import ListScope, paginate, render_list
from apps.core.responses import invalid, perform
from apps.organizations import selectors, services
from apps.organizations.forms import (
    BranchCreateForm,
    BranchFilterForm,
    BranchForm,
    BranchManagerForm,
    ScopeChangeForm,
    SelfApprovalForm,
)
from apps.organizations.models import Branch
from apps.organizations.structure_dialogs import (
    branch_activity_dialog,
    branch_edit_dialog,
    branch_manager_dialog,
    branch_url,
    company_url,
    new_branch_dialog,
    self_approval_dialog,
)
from apps.organizations.views.structure_common import ORG, section

MANAGED = ListScope(prefix="managed_", anchor="managed", sized=False)
WAREHOUSES = ListScope(prefix="warehouses_", anchor="warehouses", sized=False)

NEW_IDS = "id_new_%s"
EDIT_IDS = "id_branch_%s"
MANAGER_IDS = "id_manager_%s"
APPROVAL_IDS = "id_approval_%s"
ACTIVITY_IDS = "id_activity_%s"


@require_safe
@require_capability(ORG)
def branches(request: HttpRequest) -> HttpResponse:
    ctx = access_for(request)
    filters = BranchFilterForm(request.GET or None, ctx=ctx)
    data = filters.cleaned_data if filters.is_valid() else {}
    company = data.get("company")
    listing = paginate(
        request,
        selectors.branch_list(
            ctx,
            q=data.get("q", ""),
            company_id=company.pk if company else None,
            state=data.get("state", ""),
        ),
        default_per_page=selectors.DEFAULT_PER_PAGE,
        sorts=selectors.BRANCH_SORTS,
        default_sort="code",
        sort_labels=selectors.BRANCH_SORT_LABELS,
    )
    return render_list(
        request, "organizations/branches.html", {"filters": filters, "listing": listing}
    )


def _manager_form(ctx: AccessContext, found: Branch, *args: object) -> BranchManagerForm:
    return BranchManagerForm(
        *args,
        ctx=ctx,
        branch=found,
        preview_url=reverse("org:preview_branch_manager", args=[found.pk]),
        auto_id=MANAGER_IDS,
    )


def _page_dialogs(ctx: AccessContext, found: Branch, manages_others: bool) -> dict[str, object]:
    """The inline dialogs of the branch page (the managing branch only where it may change)."""
    manager = None
    if not manages_others:
        form = _manager_form(ctx, found)
        form.initial = {
            "managed_by": found.managed_by_id,
            "seen_generation": selectors.current_generation(ctx),
        }
        manager = branch_manager_dialog(found, form, preview=None)
    allow = not found.allow_self_approval
    return {
        "edit_dialog": branch_edit_dialog(
            found,
            BranchForm(
                initial=BranchForm.initial_for(found), ctx=ctx, branch=found, auto_id=EDIT_IDS
            ),
        ),
        "manager_dialog": manager,
        "approval_dialog": self_approval_dialog(
            found,
            SelfApprovalForm(initial={"allow": "on" if allow else "off"}, auto_id=APPROVAL_IDS),
            allow=allow,
        ),
    }


@require_safe
@require_capability(ORG)
def branch(request: HttpRequest, branch_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    found = selectors.branch(ctx, branch_id)
    _none, managed = section(request, lambda: selectors.managed_branches(ctx, found), MANAGED)
    _none, warehouses = section(request, lambda: selectors.warehouses(ctx, found), WAREHOUSES)
    manages_others = managed.total > 0
    context = {
        "branch": found,
        "managed": managed,
        "manages_others": manages_others,
        "warehouses": warehouses,
        "projects": selectors.project_counts(ctx, found),
        **_page_dialogs(ctx, found, manages_others),
    }
    return render(request, "organizations/branch.html", context)


# --- create and edit -------------------------------------------------------------------------


def _new_branch_form(request: HttpRequest, ctx: AccessContext) -> tuple[BranchCreateForm, str]:
    """The form of a new branch, its company chosen when opened from the company's page
    (``?company=``, kept only if it is one the form offers); where its Cancel leads."""
    form = BranchCreateForm(ctx=ctx, actor_id=ctx.user_id, auto_id=NEW_IDS)
    wanted = request.GET.get("company", "")
    chosen = None
    with suppress(ValueError):
        chosen = selectors.companies(ctx).filter(pk=UUID(wanted)).first()
    if chosen is None:
        return form, ""
    form.initial["company"] = chosen.pk
    return form, company_url(chosen.pk)


@require_safe
@require_capability(ORG)
def new_branch(request: HttpRequest) -> HttpResponse:
    """The dialog that creates a branch (its POST is ``create_branch``)."""
    form, back = _new_branch_form(request, access_for(request))
    return lazy(request, new_branch_dialog(form, back=back))


@mutating(ORG)
def create_branch(request: HttpRequest) -> HttpResponse:
    ctx = access_for(request)
    form = BranchCreateForm(request.POST, ctx=ctx, auto_id=NEW_IDS)
    dialog = new_branch_dialog(form).dialog()
    to = reverse("org:branches")
    if not form.is_valid():
        return invalid(request, form, to, dialog=dialog)
    return perform(
        request,
        lambda: services.create_branch(
            ctx.principal,
            form.new_branch(),
            idempotency_key=form.cleaned_data["idempotency_key"],
        ),
        lambda made: _("Branch %(branch)s created.") % {"branch": made},
        lambda made: branch_url(made.pk),
        failed_to=to,
        dialog=dialog,
    )


@mutating(ORG)
def update_branch(request: HttpRequest, branch_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    found = selectors.branch(ctx, branch_id)
    form = BranchForm(request.POST, ctx=ctx, branch=found, auto_id=EDIT_IDS)
    dialog = branch_edit_dialog(found, form).dialog()
    to = branch_url(found.pk)
    if not form.is_valid():
        return invalid(request, form, to, dialog=dialog)
    return perform(
        request,
        lambda: services.update_branch(ctx.principal, found.pk, **form.values()),
        _("Branch saved."),
        to,
        dialog=dialog,
    )


# --- the managing branch and self-approval ---------------------------------------------------


def _chosen_manager(ctx: AccessContext, found: Branch, value: str) -> tuple[bool, UUID | None]:
    """``(offered, id)`` of the managing branch a preview asks about: empty is none, any
    other value must be one of the candidates the dialog offers."""
    if not value:
        return True, None
    try:
        wanted = UUID(value)
    except ValueError:
        return False, None
    offered = selectors.manager_candidates(ctx, found).filter(pk=wanted).exists()
    return offered, wanted if offered else None


@require_safe
@require_capability(ORG)
def preview_branch_manager(request: HttpRequest, branch_id: UUID) -> HttpResponse:
    """What choosing ``?managed_by=`` would change: the dialog's preview, and the generation
    it was read at for the form (an out-of-band swap of its hidden field)."""
    ctx = access_for(request)
    found = selectors.branch(ctx, branch_id)
    offered, manager_id = _chosen_manager(ctx, found, request.GET.get("managed_by", ""))
    preview = selectors.branch_manager(ctx, found, manager_id) if offered else None
    context = {
        "data": preview,
        "generation": selectors.current_generation(ctx) if preview is None else preview.generation,
        "field_id": MANAGER_IDS % "seen_generation",
    }
    return render(request, "organizations/manager_preview_fragment.html", context)


@mutating(ORG)
def set_branch_manager(request: HttpRequest, branch_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    found = selectors.branch(ctx, branch_id)
    form = _manager_form(ctx, found, request.POST)
    dialog = branch_manager_dialog(
        found,
        form,
        preview=lambda: (
            selectors.branch_manager(ctx, found, form.manager_id()) if form.is_valid() else None
        ),
    ).dialog()
    to = branch_url(found.pk)
    if not form.is_valid():
        return invalid(request, form, to, dialog=dialog)
    return perform(
        request,
        lambda: services.update_branch(
            ctx.principal,
            found.pk,
            managed_by_id=form.manager_id(),
            seen_generation=form.cleaned_data["seen_generation"],
        ),
        _("Managing branch saved."),
        to,
        dialog=dialog,
    )


@mutating(ORG)
def set_self_approval(request: HttpRequest, branch_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    found = selectors.branch(ctx, branch_id)
    form = SelfApprovalForm(request.POST, auto_id=APPROVAL_IDS)
    allow = form.allowed() if form.is_valid() else not found.allow_self_approval
    dialog = self_approval_dialog(found, form, allow=allow).dialog()
    to = branch_url(found.pk)
    if not form.is_valid():
        return invalid(request, form, to, dialog=dialog)
    return perform(
        request,
        lambda: services.update_branch(ctx.principal, found.pk, allow_self_approval=allow),
        _("Self-approval allowed.") if allow else _("Self-approval stopped."),
        to,
        dialog=dialog,
    )


# --- activity --------------------------------------------------------------------------------


def _confirm(request: HttpRequest, branch_id: UUID, *, active: bool) -> HttpResponse:
    """The lazy dialog of an activity change, with its preview read now."""
    ctx = access_for(request)
    found = selectors.branch(ctx, branch_id)
    preview = selectors.branch_activity(ctx, found, active=active)
    form = ScopeChangeForm(initial={"seen_generation": preview.generation}, auto_id=ACTIVITY_IDS)
    return lazy(request, branch_activity_dialog(found, form, active=active, preview=preview))


@require_safe
@require_capability(ORG)
def confirm_deactivate_branch(request: HttpRequest, branch_id: UUID) -> HttpResponse:
    return _confirm(request, branch_id, active=False)


@require_safe
@require_capability(ORG)
def confirm_reactivate_branch(request: HttpRequest, branch_id: UUID) -> HttpResponse:
    return _confirm(request, branch_id, active=True)


def _set_active(request: HttpRequest, branch_id: UUID, *, active: bool) -> HttpResponse:
    ctx = access_for(request)
    found = selectors.branch(ctx, branch_id)
    form = ScopeChangeForm(request.POST, auto_id=ACTIVITY_IDS)
    dialog = branch_activity_dialog(
        found,
        form,
        active=active,
        preview=lambda: selectors.branch_activity(ctx, found, active=active),
    ).dialog()
    to = branch_url(found.pk)
    if not form.is_valid():
        return invalid(request, form, to, dialog=dialog)
    return perform(
        request,
        lambda: services.update_branch(
            ctx.principal,
            found.pk,
            is_active=active,
            seen_generation=form.cleaned_data["seen_generation"],
        ),
        _("Branch reactivated.") if active else _("Branch deactivated."),
        to,
        dialog=dialog,
    )


@mutating(ORG)
def deactivate_branch(request: HttpRequest, branch_id: UUID) -> HttpResponse:
    return _set_active(request, branch_id, active=False)


@mutating(ORG)
def reactivate_branch(request: HttpRequest, branch_id: UUID) -> HttpResponse:
    return _set_active(request, branch_id, active=True)
