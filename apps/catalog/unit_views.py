"""Units of measure (M10.3, Q72, ADR-0030): list, create, edit, deactivate and reactivate.

Group administrators only (``org.manage``; 403 otherwise): units are installation-wide master
data, so there is no scope to be out of, only the capability. Reads go through the selectors;
writes through ``create_unit`` / ``update_unit`` from POST-only views, each on its own URL,
answering by the dialog contract of ``apps.core.responses``. The services hold the rules (a
fixed ``code`` and no lower ``decimals`` once the unit is used); the form only says them first,
and a refusal reaches the dialog with the service's reason.
"""

from uuid import UUID

from django.http import HttpRequest, HttpResponse
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_safe

from apps.access.middleware import access_for
from apps.access.mixins import mutating, require_capability
from apps.access.roles import Capability
from apps.catalog import selectors, services
from apps.catalog.forms import UnitCreateForm, UnitFilterForm, UnitForm
from apps.catalog.models import UnitOfMeasure
from apps.catalog.unit_dialogs import edit_unit_dialog, new_unit_dialog, units_url
from apps.core.dialogs import lazy
from apps.core.listing import paginate, render_list
from apps.core.responses import invalid, perform

ORG = Capability.ORG_MANAGE
NEW_IDS = "id_new_%s"
EDIT_IDS = "id_unit_%s"


@require_safe
@require_capability(ORG)
def units(request: HttpRequest) -> HttpResponse:
    ctx = access_for(request)
    filters = UnitFilterForm(request.GET or None)
    data = filters.cleaned_data if filters.is_valid() else {}
    listing = paginate(
        request,
        selectors.unit_list(ctx, q=data.get("q", ""), state=data.get("state", "")),
        default_per_page=selectors.DEFAULT_PER_PAGE,
        sorts=selectors.UNIT_SORTS,
        default_sort="code",
        sort_labels=selectors.UNIT_SORT_LABELS,
    )
    return render_list(request, "catalog/units.html", {"filters": filters, "listing": listing})


@require_safe
@require_capability(ORG)
def new_unit(request: HttpRequest) -> HttpResponse:
    """The dialog that creates a unit (its POST is ``create_unit``)."""
    ctx = access_for(request)
    form = UnitCreateForm(actor_id=ctx.user_id, auto_id=NEW_IDS)
    return lazy(request, new_unit_dialog(form))


@mutating(ORG)
def create_unit(request: HttpRequest) -> HttpResponse:
    ctx = access_for(request)
    form = UnitCreateForm(request.POST, auto_id=NEW_IDS)
    dialog = new_unit_dialog(form).dialog()
    if not form.is_valid():
        return invalid(request, form, units_url(), dialog=dialog)
    return perform(
        request,
        lambda: services.create_unit(ctx.principal, **form.create_kwargs()),
        lambda made: _("Unit %(code)s created.") % {"code": made.code},
        units_url(),
        dialog=dialog,
    )


def _edit_form(unit: UnitOfMeasure, *args: object) -> tuple[UnitForm, bool]:
    """The unit's form, its fixed fields marked when anything uses the unit."""
    in_use = selectors.unit_in_use(unit)
    return UnitForm(*args, unit=unit, in_use=in_use, auto_id=EDIT_IDS), in_use


@require_safe
@require_capability(ORG)
def edit_unit(request: HttpRequest, unit_id: UUID) -> HttpResponse:
    """The dialog that changes a unit (its POST is ``update_unit``)."""
    found = selectors.unit(access_for(request), unit_id)
    form, in_use = _edit_form(found)
    return lazy(request, edit_unit_dialog(found, form, in_use=in_use))


@mutating(ORG)
def update_unit(request: HttpRequest, unit_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    found = selectors.unit(ctx, unit_id)
    form, in_use = _edit_form(found, request.POST)
    dialog = edit_unit_dialog(found, form, in_use=in_use).dialog()
    if not form.is_valid():
        return invalid(request, form, units_url(), dialog=dialog)
    return perform(
        request,
        lambda: services.update_unit(ctx.principal, found.pk, **form.values()),
        _("Unit %(code)s saved.") % {"code": found.code},
        units_url(),
        dialog=dialog,
    )


def _set_active(request: HttpRequest, unit_id: UUID, *, active: bool) -> HttpResponse:
    ctx = access_for(request)
    found = selectors.unit(ctx, unit_id)
    done = (_("Unit %(code)s reactivated.") if active else _("Unit %(code)s deactivated.")) % {
        "code": found.code
    }
    return perform(
        request,
        lambda: services.update_unit(ctx.principal, found.pk, is_active=active),
        done,
        reverse("catalog:units"),
    )


@mutating(ORG)
def deactivate_unit(request: HttpRequest, unit_id: UUID) -> HttpResponse:
    return _set_active(request, unit_id, active=False)


@mutating(ORG)
def reactivate_unit(request: HttpRequest, unit_id: UUID) -> HttpResponse:
    return _set_active(request, unit_id, active=True)
