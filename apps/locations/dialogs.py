"""The dialogs of the warehouse and worksite screens (M10.3, the M8b.4 contract).

Warehouses are on a paged list, so a new one, its data and its deactivation are lazy dialogs
(one shared dialog per kind of action, loaded for the row with htmx; a page of their own
without it). A worksite is edited from its project's page, so its dialogs are inline there.
"""

from django.forms import Form
from django.urls import reverse
from django.utils.translation import gettext as _

from apps.core.dialogs import ActionDialog
from apps.locations.forms import PlaceForm
from apps.locations.models import Location

NEW_DIALOG = "new-dialog"
EDIT_DIALOG = "edit-dialog"
ACTIVITY_DIALOG = "activity-dialog"
WORKSITE_IDS = "id_worksite_%s"


def warehouses_url() -> str:
    return reverse("locations:warehouses")


# --- warehouses --------------------------------------------------------------------------------


def new_warehouse_dialog(form: Form, *, back: str = "") -> ActionDialog:
    return ActionDialog(
        id=NEW_DIALOG,
        title=_("New warehouse"),
        icon="plus",
        action=reverse("locations:create_warehouse"),
        submit=_("Create"),
        form=form,
        note=_("To make it the branch's default, choose it on the branch's page."),
        back=back or warehouses_url(),
        testid="warehouse-new-form",
        submit_testid="warehouse-new-submit",
    )


def edit_warehouse_dialog(warehouse: Location, form: Form) -> ActionDialog:
    return ActionDialog(
        id=EDIT_DIALOG,
        title=_("Edit the warehouse"),
        description=f"{warehouse.name} · {warehouse.branch.company.code}-{warehouse.branch.code}",
        icon="pencil",
        action=reverse("locations:update_warehouse", args=[warehouse.pk]),
        submit=_("Save"),
        form=form,
        back=warehouses_url(),
        testid="warehouse-edit-form",
        submit_testid="warehouse-edit-submit",
    )


def deactivate_warehouse_dialog(warehouse: Location, form: Form) -> ActionDialog:
    """Deactivating a warehouse; the branch's default is told first, and the service refuses
    it anyway (the refusal shows on the form)."""
    return ActionDialog(
        id=ACTIVITY_DIALOG,
        title=_("Deactivate the warehouse"),
        description=f"{warehouse.name} · {warehouse.branch.company.code}-{warehouse.branch.code}",
        icon="ban",
        tone="danger",
        action=reverse("locations:deactivate_warehouse", args=[warehouse.pk]),
        submit=_("Deactivate"),
        form=form,
        consequences=(
            _("Goods can no longer be received or handed over from it."),
            _("Receipts and handovers already posted keep it. It can be reactivated."),
        ),
        warning=_(
            "It is the default warehouse of its branch: choose another default on the branch's"
            " page first."
        )
        if getattr(warehouse, "is_default", False)
        else "",
        warning_testid="default-warning",
        cancel=_("Keep it active"),
        back=warehouses_url(),
        testid="warehouse-deactivate-form",
        submit_testid="warehouse-deactivate-submit",
    )


# --- worksites (inline on the project's page) --------------------------------------------------


def worksite_edit_dialog(worksite: Location, form: Form) -> ActionDialog:
    return ActionDialog(
        id="worksite-edit-dialog",
        title=_("Edit the worksite"),
        description=worksite.name,
        icon="pencil",
        action=reverse("locations:update_worksite", args=[worksite.pk]),
        submit=_("Save"),
        form=form,
        note=_("The worksite is where the project's goods arrive."),
        testid="worksite-edit-form",
        submit_testid="worksite-edit-submit",
    )


def worksite_deactivate_dialog(worksite: Location, form: Form) -> ActionDialog:
    return ActionDialog(
        id="worksite-deactivate-dialog",
        title=_("Deactivate the worksite"),
        description=worksite.name,
        icon="ban",
        tone="danger",
        action=reverse("locations:deactivate_worksite", args=[worksite.pk]),
        submit=_("Deactivate"),
        form=form,
        consequences=(
            _("Goods can no longer be received at the project's worksite."),
            _("Receipts already posted keep it. It can be reactivated."),
        ),
        cancel=_("Keep it active"),
        testid="worksite-deactivate-form",
        submit_testid="worksite-deactivate-submit",
    )


def worksite_reactivate_dialog(worksite: Location, form: Form) -> ActionDialog:
    return ActionDialog(
        id="worksite-reactivate-dialog",
        title=_("Reactivate the worksite"),
        description=worksite.name,
        icon="rotate-ccw",
        action=reverse("locations:reactivate_worksite", args=[worksite.pk]),
        submit=_("Reactivate"),
        form=form,
        consequences=(_("Goods can be received at the project's worksite again."),),
        cancel=_("Keep it inactive"),
        testid="worksite-reactivate-form",
        submit_testid="worksite-reactivate-submit",
    )


def worksite_dialogs(worksite: Location) -> dict[str, ActionDialog]:
    """The inline dialogs of a project's page for its worksite: ``edit``, and ``activity``
    (deactivate while it is active, reactivate while it is not)."""
    activity = worksite_deactivate_dialog if worksite.is_active else worksite_reactivate_dialog
    return {
        "edit": worksite_edit_dialog(
            worksite, PlaceForm(initial=PlaceForm.initial_for(worksite), auto_id=WORKSITE_IDS)
        ),
        "activity": activity(worksite, Form(auto_id=WORKSITE_IDS)),
    }
