"""Forms of the requester's screens. Every choice comes from a selector (plan §3.6: no IDOR
through a ``<select>`` or a hidden id); the services authorize again in their transaction.

Quantities are entered in the user's language (``localize``: ``2,5`` in Spanish) and the
service checks the unit's decimals.
"""

from collections.abc import Sequence
from decimal import Decimal
from typing import Any
from uuid import UUID

from django import forms
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.access.context import AccessContext
from apps.accounts.models import User
from apps.catalog import selectors as catalog_selectors
from apps.catalog.forms import UnitChoiceField
from apps.core.forms import IdempotentForm
from apps.material_requests import selectors
from apps.material_requests.models import MaterialRequest, MaterialRequestLine

STATUS_CHOICES = [
    ("", _("All")),
    *((value, MaterialRequest.Status(value).label) for value in selectors.STATUS_FILTERS),
]


class IdChoiceField(forms.ModelChoiceField):  # type: ignore[type-arg]
    """A hidden id checked against a selector's queryset."""

    widget = forms.HiddenInput


QUANTITY = _("Quantity")


def user_label(user: User) -> str:
    return user.full_name or user.email


def seen_version_field() -> forms.IntegerField:
    """The version of the request the page showed (``selectors.version``): the services
    refuse a decision or an edit made on a page older than the request (M7b.3)."""
    return forms.IntegerField(required=False, min_value=0, widget=forms.HiddenInput)


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


class ProjectFilterField(forms.ModelChoiceField):  # type: ignore[type-arg]
    """The inbox's project filter: an optional choice among the projects the user reviews."""


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

    reviewer = forms.ChoiceField(
        label=_("Send to a reviewer"),
        required=False,
        help_text=_("Optional. Any reviewer of the branch can still decide the request."),
    )

    def __init__(self, *args: Any, reviewers: Sequence[User] = (), **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if reviewers:
            self.fields["reviewer"].choices = [  # type: ignore[attr-defined]
                ("", _("No one in particular")),
                *((str(user.pk), user_label(user)) for user in reviewers),
            ]
        else:
            del self.fields["reviewer"]

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
            "reviewer": ""
            if request.assigned_reviewer_id is None
            else request.assigned_reviewer_id,
        }

    def header(self) -> dict[str, Any]:
        data = self.cleaned_data
        return {
            "priority": data["priority"],
            "needed_by": data["needed_by"],
            "notes": data["notes"],
        }

    def reviewer_id(self) -> UUID | None:
        """The reviewer picked, if the field was offered and one was chosen."""
        value = self.cleaned_data.get("reviewer")
        return UUID(value) if value else None


class ReasonForm(forms.Form):
    """Discarding or cancelling: the reason is optional and kept in the audit trail."""

    reason = forms.CharField(label=_("Reason"), required=False, max_length=500)


# --- the reviewer's screens (M4.4) ----------------------------------------------------


class InboxFilterForm(forms.Form):
    """The approval inbox: status, priority, project and a needed-by deadline."""

    status = forms.ChoiceField(
        label=_("Status"),
        required=False,
        choices=[
            (MaterialRequest.Status.SUBMITTED, _("To review")),
            (MaterialRequest.Status.APPROVED, _("Approved")),
        ],
    )
    priority = forms.ChoiceField(
        label=_("Priority"),
        required=False,
        choices=[("", _("All")), *MaterialRequest.Priority.choices],
    )
    project = ProjectFilterField(label=_("Project"), required=False, queryset=None)
    needed_before = forms.DateField(
        label=_("Needed by"),
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text=_("On or before"),
    )
    assigned = forms.ChoiceField(
        label=_("Reviewer"),
        required=False,
        choices=[("", _("All")), ("1", _("Assigned to me"))],
    )

    def __init__(self, *args: Any, ctx: AccessContext, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        project = self.fields["project"]
        project.queryset = selectors.inbox_projects(ctx)  # type: ignore[attr-defined]
        project.empty_label = _("All")  # type: ignore[attr-defined]


class DecisionForm(forms.Form):
    """One line of the review page: approve (a quantity up to the requested) or reject."""

    decision = forms.ChoiceField(
        choices=[
            (MaterialRequestLine.Decision.APPROVED, _("Approve")),
            (MaterialRequestLine.Decision.REJECTED, _("Reject")),
        ]
    )
    approved_qty = quantity_field(_("Approved quantity"), required=False)
    note = forms.CharField(label=_("Reason"), required=False, max_length=500)
    seen_version = seen_version_field()

    def clean(self) -> dict[str, Any]:
        data: dict[str, Any] = super().clean() or {}
        if data.get("decision") == MaterialRequestLine.Decision.REJECTED and not data.get("note"):
            self.add_error("note", forms.ValidationError(_("Give a reason."), code="required"))
        return data


class ApproveAllForm(forms.Form):
    """Approve every pending line: only the version of the page, which the service checks."""

    seen_version = seen_version_field()


class ReturnForm(forms.Form):
    """Return for changes: the requester reads the note."""

    note = forms.CharField(
        label=_("What should change"),
        max_length=500,
        widget=forms.Textarea(attrs={"rows": 2}),
    )


class ReduceForm(forms.Form):
    approved_qty = quantity_field(_("New approved quantity"))
    reason = forms.CharField(label=_("Reason"), required=False, max_length=500)


class SourcingForm(forms.Form):
    sourcing = forms.ChoiceField(
        label=_("Sourcing"),
        choices=[
            (MaterialRequestLine.Sourcing.STOCK, _("From stock")),
            (MaterialRequestLine.Sourcing.PURCHASE, _("Purchase")),
        ],
    )


class ResolveForm(forms.Form):
    """The material a non-catalog line is linked to (checked against the catalog selector)."""

    material = IdChoiceField(queryset=None)

    def __init__(self, *args: Any, ctx: AccessContext, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["material"].queryset = catalog_selectors.materials(ctx)  # type: ignore[attr-defined]


class ScheduleForm(forms.Form):
    """Priority and needed-by date of an approved request; both decide the attribution
    order of receipts (an empty date clears it)."""

    priority = forms.ChoiceField(label=_("Priority"), choices=MaterialRequest.Priority.choices)
    needed_by = forms.DateField(
        label=_("Needed by"), required=False, widget=forms.DateInput(attrs={"type": "date"})
    )

    @classmethod
    def initial_for(cls, request: MaterialRequest) -> dict[str, Any]:
        return {"priority": request.priority, "needed_by": request.needed_by}


class RequiredReasonForm(forms.Form):
    """Closing and reopening, of a line or of the request, always give a reason."""

    reason = forms.CharField(
        label=_("Reason"), max_length=500, error_messages={"required": _("Give a reason.")}
    )


# --- reviewer assignment and edits (M7b.4) ---------------------------------------------

REASON_REQUIRED = {"required": _("Give a reason.")}


class AssignForm(forms.Form):
    """Send the request to another reviewer; the choices are the ones the selector offers
    and the note is required (the service judges the assignee again)."""

    reviewer = forms.ChoiceField(label=_("Reviewer"))
    note = forms.CharField(
        label=_("Note for the reviewer"),
        max_length=500,
        widget=forms.Textarea(attrs={"rows": 2}),
        error_messages={"required": _("Give a note.")},
    )

    def __init__(self, *args: Any, reviewers: Sequence[User], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["reviewer"].choices = [  # type: ignore[attr-defined]
            (str(user.pk), user_label(user)) for user in reviewers
        ]

    def reviewer_id(self) -> UUID:
        return UUID(self.cleaned_data["reviewer"])


class LineChangeForm(IdempotentForm):
    """A reviewer's change of a line, or the line they add: a catalog material (chosen on
    the page, checked against the catalog selector) or a described one with its unit, the
    quantity and the reason. ``decision`` is the "save and approve" button."""

    operation = "request_line.edit"

    material = IdChoiceField(queryset=None, required=False)
    free_text = forms.CharField(label=_("Description"), required=False, max_length=300)
    unit = UnitChoiceField(label=_("Unit"), queryset=None, required=False)
    requested_qty = quantity_field(required=False)
    reason = forms.CharField(
        label=_("Reason for the change"), max_length=500, error_messages=REASON_REQUIRED
    )
    decision = forms.ChoiceField(
        required=False, choices=[("", ""), (MaterialRequestLine.Decision.APPROVED, _("Approve"))]
    )
    seen_version = seen_version_field()

    def __init__(
        self, *args: Any, ctx: AccessContext, line: MaterialRequestLine | None = None, **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)
        self.line = line
        self.fields["material"].queryset = catalog_selectors.materials(ctx)  # type: ignore[attr-defined]
        self.fields["unit"].queryset = catalog_selectors.units(ctx)  # type: ignore[attr-defined]

    def clean(self) -> dict[str, Any]:
        data: dict[str, Any] = super().clean() or {}
        if self.line is None and data.get("requested_qty") is None:
            self.add_error(
                "requested_qty", forms.ValidationError(_("Give a quantity."), code="required")
            )
        return data

    def changes(self) -> dict[str, Any]:
        """What differs from the line (all of it for a new line): the service takes only
        the fields named."""
        if self.cleaned_data.get("material") is not None:
            found = self._material_change()
        else:
            found = self._description_changes()
        quantity = self.cleaned_data.get("requested_qty")
        if quantity is not None and self._differs(quantity, "requested_qty"):
            found["requested_qty"] = quantity
        return found

    def _differs(self, value: Any, attribute: str) -> bool:
        return self.line is None or value != getattr(self.line, attribute)

    def _material_change(self) -> dict[str, Any]:
        material = self.cleaned_data["material"]
        return {"material_id": material.pk} if self._differs(material.pk, "material_id") else {}

    def _description_changes(self) -> dict[str, Any]:
        """A described material: its text and unit, as far as they differ."""
        found: dict[str, Any] = {}
        text, unit = self.cleaned_data.get("free_text", "").strip(), self.cleaned_data.get("unit")
        if text and self._differs(text, "free_text"):
            found["free_text"] = text
        if unit is not None and self._differs(unit.pk, "unit_id"):
            found["unit_id"] = unit.pk
        return found


class ReviewerLineAddForm(LineChangeForm):
    """A line a reviewer adds to a submitted or approved request."""

    operation = "request_line.add_as_reviewer"


class LineRemoveForm(IdempotentForm):
    """Removing a line (it stays, rejected or closed short): a reason."""

    operation = "request_line.remove_as_reviewer"

    reason = forms.CharField(label=_("Reason"), max_length=500, error_messages=REASON_REQUIRED)
    seen_version = seen_version_field()
