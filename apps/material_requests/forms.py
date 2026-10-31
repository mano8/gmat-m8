"""Forms of the requester's screens. Every choice comes from a selector (plan §3.6: no IDOR
through a ``<select>`` or a hidden id); the services authorize again in their transaction.

Quantities are entered in the user's language (``localize``: ``2,5`` in Spanish) and the
service checks the unit's decimals.
"""

from decimal import Decimal
from typing import Any

from django import forms
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.access.context import AccessContext
from apps.catalog import selectors as catalog_selectors
from apps.catalog.forms import UnitChoiceField
from apps.core.forms import IdempotentForm
from apps.material_requests import selectors
from apps.material_requests.models import MaterialRequest

STATUS_CHOICES = [
    ("", _("All")),
    *((value, MaterialRequest.Status(value).label) for value in selectors.STATUS_FILTERS),
]


class IdChoiceField(forms.ModelChoiceField):  # type: ignore[type-arg]
    """A hidden id checked against a selector's queryset."""

    widget = forms.HiddenInput


QUANTITY = _("Quantity")


def quantity_field(label: Any = QUANTITY, *, required: bool = True) -> forms.DecimalField:
    return forms.DecimalField(
        label=label,
        required=required,
        max_digits=12,
        decimal_places=3,
        localize=True,
        widget=forms.TextInput(attrs={"inputmode": "decimal", "autocomplete": "off"}),
    )


class ProjectSearchForm(forms.Form):
    q = forms.CharField(label=_("Search"), required=False, max_length=100)


class MyRequestsFilterForm(forms.Form):
    status = forms.ChoiceField(label=_("Status"), required=False, choices=STATUS_CHOICES)


class MaterialSearchForm(forms.Form):
    q = forms.CharField(
        label=_("Search materials"),
        required=False,
        max_length=100,
        widget=forms.TextInput(
            attrs={"type": "search", "autocomplete": "off", "enterkeyhint": "search"}
        ),
    )


class StartForm(IdempotentForm):
    """Tapping a project: the draft is created for it (one key per project button)."""

    operation = "request.create"

    project = IdChoiceField(queryset=None)

    def __init__(self, *args: Any, ctx: AccessContext, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["project"].queryset = selectors.creatable_projects(ctx)  # type: ignore[attr-defined]


class LineAddForm(forms.Form):
    """Tapping a catalog material adds one; a given quantity adds that many."""

    material = IdChoiceField(queryset=None)
    requested_qty = quantity_field(required=False)

    def __init__(self, *args: Any, ctx: AccessContext, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["material"].queryset = catalog_selectors.materials(ctx)  # type: ignore[attr-defined]

    def quantity(self) -> Decimal:
        value: Decimal | None = self.cleaned_data["requested_qty"]
        return Decimal("1") if value is None else value


class FreeLineForm(IdempotentForm):
    """A material that is not in the catalog: described, with its unit."""

    operation = "request_line.add"

    free_text = forms.CharField(label=_("Description"), max_length=300)
    unit = UnitChoiceField(label=_("Unit"), queryset=None)
    requested_qty = quantity_field()

    def __init__(self, *args: Any, ctx: AccessContext, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["unit"].queryset = catalog_selectors.units(ctx)  # type: ignore[attr-defined]


class QuantityForm(forms.Form):
    """The stepper of a draft line: minus and plus change by one, the field sets it."""

    STEPS = {"dec": Decimal("-1"), "inc": Decimal("1")}

    requested_qty = quantity_field()
    step = forms.ChoiceField(required=False, choices=[("dec", "-"), ("inc", "+")])

    def new_quantity(self) -> Decimal:
        """The quantity typed, moved by one if minus or plus was tapped."""
        value: Decimal = self.cleaned_data["requested_qty"]
        step = self.cleaned_data["step"]
        return value + self.STEPS[step] if step else value


class SubmitForm(IdempotentForm):
    """The review step: priority, needed-by date and notes, then the submit."""

    operation = "request.submit"

    priority = forms.ChoiceField(
        label=_("Priority"),
        choices=MaterialRequest.Priority.choices,
        initial=MaterialRequest.Priority.NORMAL,
    )
    needed_by = forms.DateField(
        label=_("Needed by"),
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text=_("When the material must be on site."),
    )
    notes = forms.CharField(
        label=_("Notes"),
        required=False,
        max_length=2000,
        widget=forms.Textarea(attrs={"rows": 3}),
    )

    def clean_needed_by(self) -> Any:
        value = self.cleaned_data["needed_by"]
        if value is not None and value < timezone.localdate():
            raise forms.ValidationError(_("The date cannot be in the past."), code="past")
        return value

    @classmethod
    def initial_for(cls, request: MaterialRequest) -> dict[str, Any]:
        return {
            "priority": request.priority,
            "needed_by": request.needed_by,
            "notes": request.notes,
        }

    def header(self) -> dict[str, Any]:
        data = self.cleaned_data
        return {
            "priority": data["priority"],
            "needed_by": data["needed_by"],
            "notes": data["notes"],
        }


class ReasonForm(forms.Form):
    """Discarding or cancelling: the reason is optional and kept in the audit trail."""

    reason = forms.CharField(label=_("Reason"), required=False, max_length=500)
