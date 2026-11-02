"""Forms of the receiving screens (plan §11 M6.3). Every choice comes from a selector (plan
§3.6: no IDOR through a ``<select>`` or a hidden id): the places goods may arrive at, and the
order lines and allocations the form offers are the only fields that exist. The service
authorizes and validates again in its transaction (C7, C10, I4, §4.5 caps).

Quantities are entered in the user's language (``localize``: ``2,5`` in Spanish); the
service checks the unit's decimals.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.access.context import AccessContext
from apps.core.forms import IdempotentForm
from apps.core.templatetags.ui_tags import quantity as format_quantity
from apps.locations.models import Location
from apps.material_requests.forms import quantity_field
from apps.receiving import selectors
from apps.receiving.models import GoodsReceipt, GoodsReceiptLine
from apps.receiving.selectors import ReceiptRow, SplitChoice
from apps.receiving.services import ReceiptLineInput

ZERO = Decimal("0")


class ReceiptFilterForm(forms.Form):
    q = forms.CharField(
        label=_("Search"),
        required=False,
        max_length=100,
        widget=forms.TextInput(attrs={"type": "search", "enterkeyhint": "search"}),
    )
    status = forms.ChoiceField(
        label=_("Status"), required=False, choices=[("", _("All")), *GoodsReceipt.Status.choices]
    )
    place: Any = forms.ModelChoiceField(
        label=_("Where"), required=False, queryset=None, empty_label=_("All")
    )
    received_from = forms.DateField(
        label=_("Received from"), required=False, widget=forms.DateInput(attrs={"type": "date"})
    )
    received_to = forms.DateField(
        label=_("Received until"), required=False, widget=forms.DateInput(attrs={"type": "date"})
    )

    def __init__(self, *args: Any, ctx: AccessContext, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["place"].queryset = selectors.receipt_places(ctx)  # type: ignore[attr-defined]

    def clean(self) -> dict[str, Any]:
        data: dict[str, Any] = super().clean() or {}
        start, end = data.get("received_from"), data.get("received_to")
        if start and end and start > end:
            self.add_error("received_to", _("The end date cannot be before the start date."))
        return data


class ProjectSearchForm(forms.Form):
    q = forms.CharField(
        label=_("Search"),
        required=False,
        max_length=100,
        widget=forms.TextInput(attrs={"type": "search", "enterkeyhint": "search"}),
    )


class OrderSearchForm(forms.Form):
    q = forms.CharField(
        label=_("Order number, supplier or project"),
        required=False,
        max_length=100,
        widget=forms.TextInput(
            attrs={"type": "search", "autocomplete": "off", "enterkeyhint": "search"}
        ),
    )


class RequiredReasonForm(forms.Form):
    """Voiding a receipt: always a reason."""

    reason = forms.CharField(
        label=_("Reason"), max_length=500, error_messages={"required": _("Give a reason.")}
    )


@dataclass(frozen=True)
class Entry:
    """One order line of the receipt form with its fields."""

    row: ReceiptRow
    qty: forms.BoundField
    rejected: forms.BoundField
    issue: forms.BoundField
    note: forms.BoundField
    manual: forms.BoundField
    splits: list[tuple[SplitChoice, forms.BoundField]]

    @property
    def incident_open(self) -> bool:
        """The incident block is open when it holds something (or an error)."""
        return bool(
            self.rejected.errors
            or self.issue.errors
            or self.note.errors
            or self.rejected.value()
            or self.note.value()
            or self.issue.value() not in {None, "", GoodsReceiptLine.Issue.NONE}
        )

    @property
    def split_open(self) -> bool:
        return bool(self.manual.value() or any(field.errors for _split, field in self.splits))


class ReceiptForm(IdempotentForm):
    """Goods of one order that arrived at one place: header, then a block per order line."""

    operation = "receipt.post"

    location = forms.ChoiceField(widget=forms.HiddenInput)
    delivery_note_ref = forms.CharField(label=_("Delivery note"), required=False, max_length=100)
    notes = forms.CharField(
        label=_("Notes"), required=False, max_length=2000, widget=forms.Textarea(attrs={"rows": 3})
    )

    def __init__(
        self,
        *args: Any,
        rows: list[ReceiptRow],
        places: list[Location],
        actor_id: Any = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, actor_id=actor_id, **kwargs)
        self.rows = {row.po_line.pk: row for row in rows}
        self.fields["location"].choices = [(str(place.pk), place.name) for place in places]  # type: ignore[attr-defined]
        for line_id, row in self.rows.items():
            self._add_line_fields(line_id, row)

    def _add_line_fields(self, line_id: UUID, row: ReceiptRow) -> None:
        unit = row.unit
        self.fields[f"qty_{line_id}"] = quantity_field(
            _("Accepted (%(unit)s)") % {"unit": unit}, required=False
        )
        self.fields[f"rejected_{line_id}"] = quantity_field(
            _("Rejected (%(unit)s)") % {"unit": unit}, required=False
        )
        self.fields[f"issue_{line_id}"] = forms.ChoiceField(
            label=_("Incident"), required=False, choices=GoodsReceiptLine.Issue.choices
        )
        self.fields[f"note_{line_id}"] = forms.CharField(
            label=_("Incident note"),
            required=False,
            widget=forms.Textarea(attrs={"rows": 2}),
        )
        self.fields[f"manual_{line_id}"] = forms.BooleanField(
            label=_("Use the split below instead of the automatic one"), required=False
        )
        self.initial[f"qty_{line_id}"] = format_quantity(row.suggested)
        self.initial[f"issue_{line_id}"] = GoodsReceiptLine.Issue.NONE
        for choice in row.splits:
            name = f"split_{line_id}_{choice.allocation_id}"
            self.fields[name] = quantity_field(
                _("%(request)s · %(project)s")
                % {"request": choice.request_number, "project": choice.project_code},
                required=False,
            )
            self.initial[name] = format_quantity(choice.suggested)

    def clean_location(self) -> UUID:
        return UUID(self.cleaned_data["location"])

    def entries(self) -> list[Entry]:
        return [
            Entry(
                row=row,
                qty=self[f"qty_{line_id}"],
                rejected=self[f"rejected_{line_id}"],
                issue=self[f"issue_{line_id}"],
                note=self[f"note_{line_id}"],
                manual=self[f"manual_{line_id}"],
                splits=[
                    (choice, self[f"split_{line_id}_{choice.allocation_id}"])
                    for choice in row.splits
                ],
            )
            for line_id, row in self.rows.items()
        ]

    def _line_input(self, line_id: UUID, row: ReceiptRow) -> ReceiptLineInput | None:
        data = self.cleaned_data
        accepted = data.get(f"qty_{line_id}") or ZERO
        rejected = data.get(f"rejected_{line_id}") or ZERO
        if accepted == 0 and rejected == 0:
            return None  # the line did not arrive
        split = None
        if data.get(f"manual_{line_id}"):
            split = {
                choice.allocation_id: data.get(f"split_{line_id}_{choice.allocation_id}") or ZERO
                for choice in row.splits
            }
        return ReceiptLineInput(
            po_line_id=line_id,
            qty_received=accepted,
            qty_rejected=rejected,
            issue=data.get(f"issue_{line_id}") or GoodsReceiptLine.Issue.NONE,
            issue_note=data.get(f"note_{line_id}") or "",
            split=split,
        )

    def line_inputs(self) -> list[ReceiptLineInput]:
        """What arrived, per line (lines left at zero are not part of the receipt)."""
        inputs = (self._line_input(line_id, row) for line_id, row in self.rows.items())
        return [item for item in inputs if item is not None]

    def clean(self) -> dict[str, Any]:
        data: dict[str, Any] = super().clean() or {}
        if not self.errors and not self.line_inputs():
            raise forms.ValidationError(_("Enter what arrived of at least one line."), code="empty")
        return data
