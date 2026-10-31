"""Supplier forms. The services authorize and validate again in their transaction."""

from typing import Any

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.core.forms import IdempotentForm
from apps.suppliers import selectors
from apps.suppliers.models import Supplier


class SupplierFilterForm(forms.Form):
    q = forms.CharField(label=_("Search"), required=False, max_length=100)
    state = forms.ChoiceField(
        label=_("Status"),
        required=False,
        choices=[
            (selectors.STATE_ACTIVE, _("Active")),
            (selectors.STATE_INACTIVE, _("Inactive")),
            (selectors.STATE_ALL, _("All")),
        ],
    )


class SupplierForm(forms.Form):
    name = forms.CharField(label=_("Name"), max_length=200)
    tax_id = forms.CharField(label=_("Tax id"), max_length=32, required=False)
    contact_name = forms.CharField(label=_("Contact person"), max_length=200, required=False)
    email = forms.EmailField(label=_("Email"), required=False)
    phone = forms.CharField(label=_("Phone"), max_length=40, required=False)
    address = forms.CharField(
        label=_("Address"), required=False, widget=forms.Textarea(attrs={"rows": 2})
    )
    notes = forms.CharField(
        label=_("Notes"), required=False, widget=forms.Textarea(attrs={"rows": 3})
    )

    def values(self) -> dict[str, Any]:
        data = self.cleaned_data
        return {name: data[name] for name in self.fields if name != "idempotency_key"}

    @staticmethod
    def initial_for(supplier: Supplier) -> dict[str, Any]:
        return {
            "name": supplier.name,
            "tax_id": supplier.tax_id,
            "contact_name": supplier.contact_name,
            "email": supplier.email,
            "phone": supplier.phone,
            "address": supplier.address,
            "notes": supplier.notes,
        }


class SupplierCreateForm(IdempotentForm, SupplierForm):
    operation = "supplier.create"

    def create_kwargs(self) -> dict[str, Any]:
        return {**self.values(), "idempotency_key": self.cleaned_data["idempotency_key"]}
