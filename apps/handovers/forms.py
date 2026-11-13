"""Forms of the handover screens (plan §11 M6.3). Every choice comes from a selector (plan
§3.6: no IDOR through a ``<select>`` or a hidden id): the project's open lines, the
warehouses of the requests' branch and the people who may take the material are the only
values that exist. The service authorizes and validates again in its transaction (C7, C8,
I5, S9).

Quantities are entered in the user's language (``localize``: ``2,5`` in Spanish).
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import User
from apps.accounts.people import name_of
from apps.core.forms import IdempotentForm
from apps.handovers.models import Handover
from apps.handovers.selectors import OpenLine
from apps.handovers.services import HandoverLineInput, Receiver
from apps.locations.models import Location
from apps.material_requests.forms import quantity_field
from apps.projects.models import Project

ZERO = Decimal("0")


class HandoverFilterForm(forms.Form):
    q = forms.CharField(
        label=_("Search"),
        required=False,
        max_length=100,
        widget=forms.TextInput(attrs={"type": "search", "enterkeyhint": "search"}),
    )
    status = forms.ChoiceField(
        label=_("Status"), required=False, choices=[("", _("All")), *Handover.Status.choices]
    )
    kind = forms.ChoiceField(
        label=_("Kind"), required=False, choices=[("", _("All")), *Handover.Kind.choices]
    )
    project: Any = forms.ModelChoiceField(label=_("Project"), required=False, queryset=None)
    handed_from = forms.DateField(
        label=_("From"), required=False, widget=forms.DateInput(attrs={"type": "date"})
    )
    handed_to = forms.DateField(
        label=_("Until"), required=False, widget=forms.DateInput(attrs={"type": "date"})
    )

    def __init__(self, *args: Any, projects: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        project: Any = self.fields["project"]
        project.queryset = projects
        project.empty_label = _("All")

    def clean(self) -> dict[str, Any]:
        data: dict[str, Any] = super().clean() or {}
        start, end = data.get("handed_from"), data.get("handed_to")
        if start and end and start > end:
            self.add_error("handed_to", _("The end date cannot be before the start date."))
        return data


class RequiredReasonForm(forms.Form):
    """Voiding a handover: always a reason."""

    reason = forms.CharField(
        label=_("Reason"), max_length=500, error_messages={"required": _("Give a reason.")}
    )


@dataclass(frozen=True)
class Entry:
    """One open request line of the handover form with its quantity field."""

    row: OpenLine
    qty: forms.BoundField


@dataclass(frozen=True)
class Choices:
    """What the form offers, all from selectors: the project, its open lines, the warehouses
    the material may come from and the people who may take it."""

    project: Project
    rows: list[OpenLine]
    sources: list[Location]
    receivers: list[User]
    request_id: UUID | None = None


class HandoverForm(IdempotentForm):
    """Material of a warehouse handed over to a project: where from, to whom, how much."""

    operation = "handover.post"

    project = forms.ChoiceField(widget=forms.HiddenInput)
    request = forms.CharField(required=False, widget=forms.HiddenInput)
    source = forms.ChoiceField(label=_("Take it from"))
    receiver_user = forms.ChoiceField(label=_("Received by"), required=False)
    receiver_name = forms.CharField(
        label=_("Or type the name of who takes it"), required=False, max_length=200
    )
    acknowledge = forms.BooleanField(
        label=_("The material is in the warehouse: hand it over anyway."), required=False
    )

    def __init__(self, *args: Any, choices: Choices, actor_id: Any = None, **kwargs: Any) -> None:
        super().__init__(*args, actor_id=actor_id, **kwargs)
        self.rows = {row.line.pk: row for row in choices.rows}
        self.fields["project"].choices = [(str(choices.project.pk), choices.project.code)]  # type: ignore[attr-defined]
        self.fields["source"].choices = [  # type: ignore[attr-defined]
            (str(place.pk), f"{place.branch.name} · {place.name}") for place in choices.sources
        ]
        self.fields["receiver_user"].choices = [  # type: ignore[attr-defined]
            ("", "—"),
            *((str(user.pk), name_of(user)) for user in choices.receivers),
        ]
        self.initial["project"] = str(choices.project.pk)
        self.initial["request"] = str(choices.request_id) if choices.request_id else ""
        for line_id in self.rows:
            self.fields[f"qty_{line_id}"] = quantity_field(_("Quantity"), required=False)

    def entries(self) -> list[Entry]:
        return [Entry(row, self[f"qty_{line_id}"]) for line_id, row in self.rows.items()]

    def quantities(self) -> dict[UUID, Decimal]:
        """Request line → quantity handed over (lines left empty or at zero are left out)."""
        data = self.cleaned_data
        found = {line_id: data.get(f"qty_{line_id}") or ZERO for line_id in self.rows}
        return {line_id: qty for line_id, qty in found.items() if qty > 0}

    def line_inputs(self) -> list[HandoverLineInput]:
        return [HandoverLineInput(line_id, qty) for line_id, qty in self.quantities().items()]

    def receiver(self) -> Receiver:
        data = self.cleaned_data
        user_id = data.get("receiver_user")
        return Receiver(
            user_id=UUID(user_id) if user_id else None, name=data.get("receiver_name", "")
        )

    def source_id(self) -> UUID:
        return UUID(self.cleaned_data["source"])

    def clean(self) -> dict[str, Any]:
        data: dict[str, Any] = super().clean() or {}
        if not self.errors and not self.quantities():
            raise forms.ValidationError(
                _("Enter the quantity handed over for at least one line."), code="empty"
            )
        return data
