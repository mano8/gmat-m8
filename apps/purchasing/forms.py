"""Forms of the purchasing screens. Every choice comes from a selector (plan §3.6: no IDOR
through a ``<select>`` or a hidden id); the services authorize and validate again in their
transaction (C5, C6, §4.4).

Quantities and prices are entered in the user's language (``localize``: ``2,5`` in Spanish);
the service checks the unit's decimals. A price field exists only for who holds ``price.view``.
"""

from dataclasses import dataclass
from decimal import Decimal
from types import EllipsisType
from typing import Any
from uuid import UUID

from django import forms
from django.forms import BoundField
from django.utils.translation import gettext_lazy as _

from apps.access.context import AccessContext
from apps.access.roles import Capability
from apps.core.forms import IdempotentForm
from apps.core.idempotency import issue_key
from apps.core.templatetags.ui_tags import quantity as format_quantity
from apps.material_requests.forms import quantity_field
from apps.material_requests.models import MaterialRequest
from apps.organizations import selectors as org_selectors
from apps.purchasing import selectors
from apps.purchasing.models import PurchaseAllocation, PurchaseOrder, PurchaseOrderLine
from apps.purchasing.selectors import WorklistRow
from apps.purchasing.services import AllocationAmendment, LineAmendment, RequestItem
from apps.suppliers import selectors as supplier_selectors

ORDER_STATUS_CHOICES = [
    ("", _("All")),
    (PurchaseOrder.Status.ISSUED, _("Pending")),
    (PurchaseOrder.Status.DRAFT, _("Draft")),
    (PurchaseOrder.Status.CLOSED, _("Closed")),
    (PurchaseOrder.Status.CANCELLED, _("Cancelled")),
]


def price_field(label: Any) -> forms.DecimalField:
    return forms.DecimalField(
        label=label,
        required=False,
        min_value=0,
        max_digits=14,
        decimal_places=4,
        localize=True,
        widget=forms.TextInput(attrs={"inputmode": "decimal", "autocomplete": "off"}),
    )


def discount_field() -> forms.DecimalField:
    return forms.DecimalField(
        label=_("Discount (%)"),
        required=False,
        min_value=0,
        max_value=100,
        max_digits=5,
        decimal_places=2,
        localize=True,
        widget=forms.TextInput(attrs={"inputmode": "decimal", "autocomplete": "off"}),
    )


class SupplierField(forms.ModelChoiceField):  # type: ignore[type-arg]
    """An active supplier the user may read (``supplier.view``)."""


class ShipToField(forms.ModelChoiceField):  # type: ignore[type-arg]
    def label_from_instance(self, obj: Any) -> str:
        return f"{obj.branch.company.code} · {obj.branch.name} · {obj.name}"


class BranchField(forms.ModelChoiceField):  # type: ignore[type-arg]
    def label_from_instance(self, obj: Any) -> str:
        return f"{obj.company.code} · {obj.name}"


# --- lists --------------------------------------------------------------------------------


class OrdersFilterForm(forms.Form):
    status = forms.ChoiceField(
        label=_("Status"), required=False, choices=ORDER_STATUS_CHOICES, initial="ISSUED"
    )
    supplier = SupplierField(label=_("Supplier"), required=False, queryset=None)

    def __init__(self, *args: Any, ctx: AccessContext, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        supplier = self.fields["supplier"]
        supplier.queryset = supplier_selectors.suppliers(ctx)  # type: ignore[attr-defined]
        supplier.empty_label = _("All")  # type: ignore[attr-defined]


class WorklistFilterForm(forms.Form):
    """Project and priority narrow the lines to buy; the supplier narrows the drafts and the
    order lines with quantity on order for nobody."""

    project: Any = forms.ModelChoiceField(label=_("Project"), required=False, queryset=None)
    priority = forms.ChoiceField(
        label=_("Priority"),
        required=False,
        choices=[("", _("All")), *MaterialRequest.Priority.choices],
    )
    supplier = SupplierField(label=_("Supplier"), required=False, queryset=None)

    def __init__(self, *args: Any, ctx: AccessContext, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        project: Any = self.fields["project"]
        project.queryset = selectors.worklist_projects(ctx)
        project.empty_label = _("All")
        supplier = self.fields["supplier"]
        supplier.queryset = supplier_selectors.suppliers(ctx)  # type: ignore[attr-defined]
        supplier.empty_label = _("All")  # type: ignore[attr-defined]


# --- request lines to buy: a tick and a quantity ----------------------------------------------


@dataclass(frozen=True)
class Entry:
    """One request line of an items form with its two fields."""

    row: WorklistRow
    pick: BoundField
    qty: BoundField


class ItemsMixin(forms.Form):
    """Request lines to purchase: a tick and an optional quantity each (default: all that is
    left to buy). Only the ``rows`` given exist as fields, so no other line can be named."""

    def __init__(
        self,
        *args: Any,
        rows: list[WorklistRow],
        picked: set[UUID] | None = None,
        prefix_name: str = "",
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.rows = {row.line.pk: row for row in rows}
        self.item_prefix = prefix_name
        for line_id, row in self.rows.items():
            self.fields[self._name("pick", line_id)] = forms.BooleanField(
                required=False, label=_("Add")
            )
            self.fields[self._name("qty", line_id)] = quantity_field(required=False)
            self.initial[self._name("qty", line_id)] = format_quantity(row.to_purchase)
            self.initial[self._name("pick", line_id)] = line_id in (picked or set())

    def _name(self, kind: str, line_id: UUID) -> str:
        return f"{self.item_prefix}{kind}_{line_id}"

    def entries(self) -> list[Entry]:
        return [
            Entry(row, self[self._name("pick", line_id)], self[self._name("qty", line_id)])
            for line_id, row in self.rows.items()
        ]

    def items(self) -> list[RequestItem]:
        """The ticked lines with the quantity typed (``None``: the service's default)."""
        data = self.cleaned_data
        return [
            RequestItem(line_id, data.get(self._name("qty", line_id)))
            for line_id in self.rows
            if data.get(self._name("pick", line_id))
        ]


class ItemsForm(ItemsMixin, IdempotentForm):
    """Adding request lines to a draft."""

    operation = "purchase_order.add_request_lines"

    def clean(self) -> dict[str, Any]:
        data: dict[str, Any] = super().clean() or {}
        if not self.items():
            raise forms.ValidationError(_("Choose at least one request line."), code="empty")
        return data


# --- the new order --------------------------------------------------------------------------


class NewOrderForm(ItemsMixin, IdempotentForm):
    """The builder's first step: who buys, from whom, where to, and the lines chosen."""

    operation = "purchase_order.create"

    branch = BranchField(label=_("Buying branch"), queryset=None)
    supplier = SupplierField(label=_("Supplier"), queryset=None)
    ship_to = ShipToField(label=_("Ship to"), queryset=None)
    expected_date = forms.DateField(
        label=_("Expected date"), required=False, widget=forms.DateInput(attrs={"type": "date"})
    )
    supplier_ref = forms.CharField(label=_("Supplier reference"), required=False, max_length=100)
    notes = forms.CharField(
        label=_("Notes"), required=False, max_length=2000, widget=forms.Textarea(attrs={"rows": 3})
    )

    # The lines of the order are added with a key of their own: a double tap replays both.
    items_key = forms.CharField(required=False, widget=forms.HiddenInput)

    def __init__(self, *args: Any, ctx: AccessContext, actor_id: Any = None, **kwargs: Any) -> None:
        super().__init__(*args, actor_id=actor_id, **kwargs)
        self.ctx = ctx
        if not self.is_bound and actor_id is not None:
            self.initial["items_key"] = issue_key(actor_id, ItemsForm.operation)
        self.fields["branch"].queryset = org_selectors.branches(ctx, Capability.PO_MANAGE)  # type: ignore[attr-defined]
        self.fields["supplier"].queryset = supplier_selectors.suppliers(ctx)  # type: ignore[attr-defined]
        self.fields["ship_to"].queryset = selectors.ship_to_choices(ctx)  # type: ignore[attr-defined]

    def clean(self) -> dict[str, Any]:
        data: dict[str, Any] = super().clean() or {}
        branch, ship_to = data.get("branch"), data.get("ship_to")
        if branch is not None and ship_to is not None:
            valid = selectors.ship_to_choices(self.ctx, branch_id=branch.pk)
            if not valid.filter(pk=ship_to.pk).exists():
                self.add_error("ship_to", forms.ValidationError(_("Choose a valid destination.")))
        if branch is not None:
            elsewhere = [
                row
                for row in self.rows_chosen()
                if row.line.request.procurement_branch_id != branch.pk
            ]
            if elsewhere:
                raise forms.ValidationError(
                    _("Request %(number)s is bought for by another branch."),
                    code="branch",
                    params={"number": elsewhere[0].line.request.number},
                )
        return data

    def rows_chosen(self) -> list[WorklistRow]:
        return [self.rows[item.request_line_id] for item in self.items()]

    def header(self) -> dict[str, Any]:
        data = self.cleaned_data
        return {
            "branch_id": data["branch"].pk,
            "supplier_id": data["supplier"].pk,
            "ship_to_id": data["ship_to"].pk,
            "expected_date": data["expected_date"],
            "supplier_ref": data["supplier_ref"],
            "notes": data["notes"],
        }


# --- a draft ----------------------------------------------------------------------------------


class DraftHeaderForm(forms.Form):
    """A draft's supplier, destination, expected date, reference and notes."""

    supplier = SupplierField(label=_("Supplier"), queryset=None)
    ship_to = ShipToField(label=_("Ship to"), queryset=None)
    expected_date = forms.DateField(
        label=_("Expected date"), required=False, widget=forms.DateInput(attrs={"type": "date"})
    )
    supplier_ref = forms.CharField(label=_("Supplier reference"), required=False, max_length=100)
    notes = forms.CharField(
        label=_("Notes"), required=False, max_length=2000, widget=forms.Textarea(attrs={"rows": 3})
    )

    def __init__(self, *args: Any, ctx: AccessContext, order: PurchaseOrder, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["supplier"].queryset = supplier_selectors.suppliers(ctx)  # type: ignore[attr-defined]
        self.fields["ship_to"].queryset = selectors.ship_to_choices(  # type: ignore[attr-defined]
            ctx, branch_id=order.branch_id
        )

    @classmethod
    def initial_for(cls, order: PurchaseOrder) -> dict[str, Any]:
        return {
            "supplier": order.supplier_id,
            "ship_to": order.ship_to_id,
            "expected_date": order.expected_date,
            "supplier_ref": order.supplier_ref,
            "notes": order.notes,
        }

    def changes(self) -> dict[str, Any]:
        data = self.cleaned_data
        return {
            "supplier_id": data["supplier"].pk,
            "ship_to_id": data["ship_to"].pk,
            "expected_date": data["expected_date"],
            "supplier_ref": data["supplier_ref"],
            "notes": data["notes"],
        }


class LineForm(forms.Form):
    """A draft line: its ordered quantity and, for who holds ``price.view``, its price.

    A price field left empty clears the price; without ``price.view`` the fields do not
    exist and the prices stay as they are.
    """

    ordered_qty = quantity_field(_("Ordered quantity"))
    unit_price = price_field(_("Unit price"))
    discount_pct = discount_field()

    def __init__(self, *args: Any, prices: bool, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.prices = prices
        if not prices:
            del self.fields["unit_price"]
            del self.fields["discount_pct"]

    @classmethod
    def initial_for(cls, line: PurchaseOrderLine, *, prices: bool) -> dict[str, Any]:
        initial: dict[str, Any] = {"ordered_qty": format_quantity(line.ordered_qty)}
        if prices:
            initial |= {"unit_price": line.unit_price, "discount_pct": line.discount_pct}
        return initial

    def service_kwargs(self) -> dict[str, Any]:
        data = self.cleaned_data
        kwargs: dict[str, Any] = {"ordered_qty": data["ordered_qty"]}
        if self.prices:
            kwargs |= {"unit_price": data["unit_price"], "discount_pct": data["discount_pct"]}
        return kwargs


class AllocationQuantityForm(forms.Form):
    """The new ceiling of an allocation: raised (C5) or lowered (a trim; zero removes it)."""

    qty = quantity_field(_("Quantity"))


class IssueForm(IdempotentForm):
    operation = "purchase_order.issue"

    confirm = forms.BooleanField(
        label=_("I have checked the lines, quantities and destination."),
        error_messages={"required": _("Tick the box to issue the order.")},
    )


class ReasonForm(forms.Form):
    """Discarding a draft: the reason is optional and kept in the audit trail."""

    reason = forms.CharField(label=_("Reason"), required=False, max_length=500)


class RequiredReasonForm(forms.Form):
    """Cancelling an issued order or closing a line short: always a reason."""

    reason = forms.CharField(
        label=_("Reason"), max_length=500, error_messages={"required": _("Give a reason.")}
    )


class DispatchNoteForm(forms.Form):
    """Confirming or resending a supplier dispatch: what the supplier said, for the audit."""

    note = forms.CharField(label=_("Note"), required=False, max_length=500)


# --- the amendment of an issued order -----------------------------------------------------------


@dataclass(frozen=True)
class LineEntry:
    line: PurchaseOrderLine
    qty: BoundField
    unit_price: BoundField | None
    discount_pct: BoundField | None


@dataclass(frozen=True)
class AllocationEntry:
    allocation: PurchaseAllocation
    qty: BoundField


def _same(before: Decimal | None, after: Decimal | None) -> bool:
    return (before is None and after is None) or (
        before is not None and after is not None and before == after
    )


class AmendForm(ItemsMixin, IdempotentForm):
    """An amendment (§4.4): a reason, the header fields, quantities and prices of the lines,
    the ceilings of the allocations and request lines to add. Only what differs from the
    order is sent to ``amend``."""

    operation = "purchase_order.amend"

    reason = forms.CharField(
        label=_("Reason"), max_length=500, error_messages={"required": _("Give a reason.")}
    )
    expected_date = forms.DateField(
        label=_("Expected date"), required=False, widget=forms.DateInput(attrs={"type": "date"})
    )
    supplier_ref = forms.CharField(label=_("Supplier reference"), required=False, max_length=100)
    notes = forms.CharField(
        label=_("Notes"), required=False, max_length=2000, widget=forms.Textarea(attrs={"rows": 3})
    )

    def __init__(
        self,
        *args: Any,
        order: PurchaseOrder,
        lines: list[PurchaseOrderLine],
        allocations: list[PurchaseAllocation],
        prices: bool,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, prefix_name="add_", **kwargs)
        self.order = order
        self.prices = prices
        self.lines = {line.pk: line for line in lines}
        self.allocations = {allocation.pk: allocation for allocation in allocations}
        self.initial.update(
            expected_date=order.expected_date, supplier_ref=order.supplier_ref, notes=order.notes
        )
        for line in lines:
            self.fields[f"qty_{line.pk}"] = quantity_field(_("Ordered quantity"))
            self.initial[f"qty_{line.pk}"] = format_quantity(line.ordered_qty)
            if prices:
                self.fields[f"price_{line.pk}"] = price_field(_("Unit price"))
                self.fields[f"discount_{line.pk}"] = discount_field()
                self.initial[f"price_{line.pk}"] = line.unit_price
                self.initial[f"discount_{line.pk}"] = line.discount_pct
        for allocation in allocations:
            self.fields[f"alloc_{allocation.pk}"] = quantity_field(_("Allocated"))
            self.initial[f"alloc_{allocation.pk}"] = format_quantity(allocation.qty)

    def line_entries(self) -> list[LineEntry]:
        return [
            LineEntry(
                line,
                self[f"qty_{line_id}"],
                self[f"price_{line_id}"] if self.prices else None,
                self[f"discount_{line_id}"] if self.prices else None,
            )
            for line_id, line in self.lines.items()
        ]

    def allocation_entry(self, allocation: PurchaseAllocation) -> AllocationEntry:
        return AllocationEntry(allocation, self[f"alloc_{allocation.pk}"])

    def line_amendments(self) -> list[LineAmendment]:
        data, amendments = self.cleaned_data, []
        for line_id, line in self.lines.items():
            qty: Decimal = data[f"qty_{line_id}"]
            price: Decimal | EllipsisType | None = ...
            discount: Decimal | EllipsisType | None = ...
            if self.prices:
                new_price, new_discount = data[f"price_{line_id}"], data[f"discount_{line_id}"]
                price = ... if _same(line.unit_price, new_price) else new_price
                discount = ... if _same(line.discount_pct, new_discount) else new_discount
            if qty != line.ordered_qty or price is not ... or discount is not ...:
                amendments.append(
                    LineAmendment(
                        line_id,
                        ordered_qty=qty if qty != line.ordered_qty else None,
                        unit_price=price,
                        discount_pct=discount,
                    )
                )
        return amendments

    def allocation_amendments(self) -> list[AllocationAmendment]:
        data = self.cleaned_data
        return [
            AllocationAmendment(pk, data[f"alloc_{pk}"])
            for pk, allocation in self.allocations.items()
            if data[f"alloc_{pk}"] != allocation.qty
        ]

    def header_changes(self) -> dict[str, Any]:
        """The header fields that differ (``expected_date`` ``...`` when unchanged)."""
        data = self.cleaned_data
        changes: dict[str, Any] = {"expected_date": ...}
        if data["expected_date"] != self.order.expected_date:
            changes["expected_date"] = data["expected_date"]
        if data["supplier_ref"].strip() != self.order.supplier_ref:
            changes["supplier_ref"] = data["supplier_ref"]
        if data["notes"].strip() != self.order.notes:
            changes["notes"] = data["notes"]
        return changes
