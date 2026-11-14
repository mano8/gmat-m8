"""Warehouses and worksites (M10.3, ADR-0030): the screens of group administrators.

Every page and action needs ``org.manage`` (403 otherwise; a place out of scope is 404).
Warehouses: a paged list, create, edit, deactivate and reactivate. A worksite is created with
its project and changed from the project's page (name, address, activity). Reads go through
the selectors; writes through ``apps.locations.services`` from POST-only views, each on its
own URL, answering by the dialog contract of ``apps.core.responses``. The service holds the
rules (the branch's default warehouse stays active); its refusal reaches the dialog.
"""

from contextlib import suppress
from uuid import UUID

from django.forms import Form
from django.http import HttpRequest, HttpResponse
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_safe

from apps.access.context import AccessContext
from apps.access.middleware import access_for
from apps.access.mixins import mutating, require_capability
from apps.access.roles import Capability
from apps.core.dialogs import lazy
from apps.core.listing import paginate, render_list
from apps.core.responses import invalid, perform
from apps.locations import selectors, services
from apps.locations.dialogs import (
    WORKSITE_IDS,
    deactivate_warehouse_dialog,
    edit_warehouse_dialog,
    new_warehouse_dialog,
    warehouses_url,
    worksite_deactivate_dialog,
    worksite_edit_dialog,
    worksite_reactivate_dialog,
)
from apps.locations.forms import PlaceForm, WarehouseCreateForm, WarehouseFilterForm
from apps.locations.models import Location
from apps.projects.row_dialogs import project_url

ORG = Capability.ORG_MANAGE
NEW_IDS = "id_new_%s"
EDIT_IDS = "id_warehouse_%s"
ACTIVITY_IDS = "id_activity_%s"


# --- warehouses ------------------------------------------------------------------------------


@require_safe
@require_capability(ORG)
def warehouses(request: HttpRequest) -> HttpResponse:
    ctx = access_for(request)
    filters = WarehouseFilterForm(request.GET or None, ctx=ctx)
    data = filters.cleaned_data if filters.is_valid() else {}
    branch = data.get("branch")
    listing = paginate(
        request,
        selectors.warehouses(
            ctx,
            q=data.get("q", ""),
            branch_id=branch.pk if branch else None,
            state=data.get("state", ""),
        ),
        default_per_page=selectors.DEFAULT_PER_PAGE,
        sorts=selectors.WAREHOUSE_SORTS,
        default_sort="branch",
        sort_labels=selectors.WAREHOUSE_SORT_LABELS,
    )
    return render_list(
        request, "locations/warehouses.html", {"filters": filters, "listing": listing}
    )


def _new_form(request: HttpRequest, ctx: AccessContext) -> tuple[WarehouseCreateForm, str]:
    """The form of a new warehouse, its branch chosen when opened from the branch's page
    (``?branch=``, kept only if it is one the form offers); where its Cancel leads."""
    form = WarehouseCreateForm(ctx=ctx, actor_id=ctx.user_id, auto_id=NEW_IDS)
    chosen = None
    with suppress(ValueError):
        chosen = (
            selectors.new_warehouse_branches(ctx)
            .filter(pk=UUID(request.GET.get("branch", "")))
            .first()
        )
    if chosen is None:
        return form, ""
    form.initial["branch"] = chosen.pk
    return form, reverse("org:branch", args=[chosen.pk])


@require_safe
@require_capability(ORG)
def new_warehouse(request: HttpRequest) -> HttpResponse:
    """The dialog that creates a warehouse (its POST is ``create_warehouse``)."""
    form, back = _new_form(request, access_for(request))
    return lazy(request, new_warehouse_dialog(form, back=back))


@mutating(ORG)
def create_warehouse(request: HttpRequest) -> HttpResponse:
    ctx = access_for(request)
    form = WarehouseCreateForm(request.POST, ctx=ctx, auto_id=NEW_IDS)
    dialog = new_warehouse_dialog(form).dialog()
    if not form.is_valid():
        return invalid(request, form, warehouses_url(), dialog=dialog)
    return perform(
        request,
        lambda: services.create_warehouse(ctx.principal, **form.create_kwargs()),
        lambda made: _("Warehouse %(name)s created.") % {"name": made.name},
        warehouses_url(),
        dialog=dialog,
    )


@require_safe
@require_capability(ORG)
def edit_warehouse(request: HttpRequest, location_id: UUID) -> HttpResponse:
    """The dialog that changes a warehouse's name and address (POST: ``update_warehouse``)."""
    found = selectors.warehouse(access_for(request), location_id)
    form = PlaceForm(initial=PlaceForm.initial_for(found), auto_id=EDIT_IDS)
    return lazy(request, edit_warehouse_dialog(found, form))


@mutating(ORG)
def update_warehouse(request: HttpRequest, location_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    found = selectors.warehouse(ctx, location_id)
    form = PlaceForm(request.POST, auto_id=EDIT_IDS)
    dialog = edit_warehouse_dialog(found, form).dialog()
    if not form.is_valid():
        return invalid(request, form, warehouses_url(), dialog=dialog)
    return perform(
        request,
        lambda: services.update_location(ctx.principal, found.pk, **form.values()),
        _("Warehouse %(name)s saved.") % {"name": found.name},
        warehouses_url(),
        dialog=dialog,
    )


@require_safe
@require_capability(ORG)
def confirm_deactivate_warehouse(request: HttpRequest, location_id: UUID) -> HttpResponse:
    found = selectors.warehouse(access_for(request), location_id)
    return lazy(request, deactivate_warehouse_dialog(found, Form(auto_id=ACTIVITY_IDS)))


@mutating(ORG)
def deactivate_warehouse(request: HttpRequest, location_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    found = selectors.warehouse(ctx, location_id)
    form = Form(request.POST, auto_id=ACTIVITY_IDS)
    form.is_valid()  # no fields: the service's refusal can then be shown on it
    return perform(
        request,
        lambda: services.update_location(ctx.principal, found.pk, is_active=False),
        _("Warehouse %(name)s deactivated.") % {"name": found.name},
        warehouses_url(),
        dialog=deactivate_warehouse_dialog(found, form).dialog(),
    )


@mutating(ORG)
def reactivate_warehouse(request: HttpRequest, location_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    found = selectors.warehouse(ctx, location_id)
    return perform(
        request,
        lambda: services.update_location(ctx.principal, found.pk, is_active=True),
        _("Warehouse %(name)s reactivated.") % {"name": found.name},
        warehouses_url(),
    )


# --- worksites: changed from the project's page ----------------------------------------------


def _worksite_action(request: HttpRequest, location_id: UUID) -> tuple[Location, str]:
    """The worksite (404 unless in scope) and the project page every outcome goes back to."""
    found = selectors.worksite(access_for(request), location_id)
    assert found.project_id is not None  # noqa: S101  a worksite always has its project
    return found, project_url(found.project_id)


@mutating(ORG)
def update_worksite(request: HttpRequest, location_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    found, to = _worksite_action(request, location_id)
    form = PlaceForm(request.POST, auto_id=WORKSITE_IDS)
    dialog = worksite_edit_dialog(found, form).dialog()
    if not form.is_valid():
        return invalid(request, form, to, dialog=dialog)
    return perform(
        request,
        lambda: services.update_location(ctx.principal, found.pk, **form.values()),
        _("Worksite saved."),
        to,
        dialog=dialog,
    )


def _set_worksite_active(request: HttpRequest, location_id: UUID, *, active: bool) -> HttpResponse:
    ctx = access_for(request)
    found, to = _worksite_action(request, location_id)
    form = Form(request.POST, auto_id=WORKSITE_IDS)
    form.is_valid()  # no fields: the service's refusal can then be shown on it
    build = worksite_reactivate_dialog if active else worksite_deactivate_dialog
    return perform(
        request,
        lambda: services.update_location(ctx.principal, found.pk, is_active=active),
        _("Worksite reactivated.") if active else _("Worksite deactivated."),
        to,
        dialog=build(found, form).dialog(),
    )


@mutating(ORG)
def deactivate_worksite(request: HttpRequest, location_id: UUID) -> HttpResponse:
    return _set_worksite_active(request, location_id, active=False)


@mutating(ORG)
def reactivate_worksite(request: HttpRequest, location_id: UUID) -> HttpResponse:
    return _set_worksite_active(request, location_id, active=True)
