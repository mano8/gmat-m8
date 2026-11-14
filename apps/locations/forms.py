"""Forms of the warehouse and worksite screens (M10.3). Every choice comes from a selector;
the services authorize and validate again in their transaction."""

from typing import Any

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.access.context import AccessContext
from apps.core.forms import IdempotentForm
from apps.locations import selectors
from apps.locations.models import Location
from apps.projects.forms import BranchChoiceField

STATE_CHOICES = [
    ("", _("All")),
    (selectors.STATE_ACTIVE, _("Active")),
    (selectors.STATE_INACTIVE, _("Inactive")),
]


class WarehouseFilterForm(forms.Form):
    q = forms.CharField(label=_("Search"), required=False, max_length=100)
    branch = BranchChoiceField(
        label=_("Branch"), queryset=None, required=False, empty_label=_("All branches")
    )
    state = forms.ChoiceField(label=_("Status"), required=False, choices=STATE_CHOICES)

    def __init__(self, *args: Any, ctx: AccessContext, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["branch"].queryset = selectors.branch_filter_choices(ctx)  # type: ignore[attr-defined]


class PlaceForm(forms.Form):
    """The data of a warehouse or a worksite: its name and address."""

    name = forms.CharField(label=_("Name"), max_length=200)
    address = forms.CharField(
        label=_("Address"), required=False, widget=forms.Textarea(attrs={"rows": 2})
    )

    def values(self) -> dict[str, Any]:
        return {"name": self.cleaned_data["name"], "address": self.cleaned_data["address"]}

    @staticmethod
    def initial_for(place: Location) -> dict[str, Any]:
        return {"name": place.name, "address": place.address}


class WarehouseCreateForm(IdempotentForm, PlaceForm):
    """A new warehouse of an active branch."""

    operation = "location.create_warehouse"

    branch = BranchChoiceField(label=_("Branch"), queryset=None)
    field_order = ["branch", "name", "address"]

    def __init__(self, *args: Any, ctx: AccessContext, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["branch"].queryset = selectors.new_warehouse_branches(ctx)  # type: ignore[attr-defined]

    def create_kwargs(self) -> dict[str, Any]:
        return {
            "branch_id": self.cleaned_data["branch"].pk,
            **self.values(),
            "idempotency_key": self.cleaned_data["idempotency_key"],
        }
