"""The dialogs of the units of measure screens (M10.3, the M8b.4 contract).

A new unit and a unit's data are lazy dialogs: the list is paged, so one shared dialog per
kind of action is loaded for the row with htmx (a page of its own without it).
"""

from django.forms import Form
from django.urls import reverse
from django.utils.translation import gettext as _

from apps.catalog.models import UnitOfMeasure
from apps.core.dialogs import ActionDialog

NEW_DIALOG = "new-dialog"
EDIT_DIALOG = "edit-dialog"


def units_url() -> str:
    return reverse("catalog:units")


def new_unit_dialog(form: Form) -> ActionDialog:
    return ActionDialog(
        id=NEW_DIALOG,
        title=_("New unit of measure"),
        icon="plus",
        action=reverse("catalog:create_unit"),
        submit=_("Create"),
        form=form,
        note=_("A unit is never deleted: it can be deactivated, and its history stays."),
        back=units_url(),
        testid="unit-new-form",
        submit_testid="unit-new-submit",
    )


def edit_unit_dialog(unit: UnitOfMeasure, form: Form, *, in_use: bool) -> ActionDialog:
    return ActionDialog(
        id=EDIT_DIALOG,
        title=_("Edit the unit"),
        description=unit.label,
        icon="pencil",
        action=reverse("catalog:update_unit", args=[unit.pk]),
        submit=_("Save"),
        form=form,
        note=_("Materials, requests and orders that use this unit keep their quantities.")
        if in_use
        else "",
        back=units_url(),
        testid="unit-edit-form",
        submit_testid="unit-edit-submit",
    )
