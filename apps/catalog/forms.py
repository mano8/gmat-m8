"""Catalog forms. Every choice comes from a selector (plan §3.6: no IDOR through a
``<select>``); the services authorize and validate again in their transaction."""

from typing import Any

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.access.context import AccessContext
from apps.catalog import selectors
from apps.catalog.models import EAN_PATTERN, Material, MaterialCategory
from apps.core.forms import IdempotentForm


class CategoryChoiceField(forms.ModelChoiceField):  # type: ignore[type-arg]
    def label_from_instance(self, obj: Any) -> str:
        return str(obj)


class UnitChoiceField(forms.ModelChoiceField):  # type: ignore[type-arg]
    def label_from_instance(self, obj: Any) -> str:
        return str(obj.label)


class MaterialFilterForm(forms.Form):
    q = forms.CharField(label=_("Search"), required=False, max_length=100)
    category = CategoryChoiceField(
        label=_("Category"), queryset=None, required=False, empty_label=_("All categories")
    )
    state = forms.ChoiceField(
        label=_("Status"),
        required=False,
        choices=[
            (selectors.STATE_ACTIVE, _("Active")),
            (selectors.STATE_INACTIVE, _("Inactive")),
            (selectors.STATE_ALL, _("All")),
        ],
    )

    def __init__(self, *args: Any, ctx: AccessContext, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["category"].queryset = selectors.categories(ctx)  # type: ignore[attr-defined]


class MaterialForm(forms.Form):
    """Edit: everything but the fixed ``reference``. ``MaterialCreateForm`` adds it."""

    name = forms.CharField(label=_("Name"), max_length=200)
    unit = UnitChoiceField(label=_("Unit of measure"), queryset=None)
    category = CategoryChoiceField(label=_("Category"), queryset=None, required=False)
    description = forms.CharField(
        label=_("Description"), required=False, widget=forms.Textarea(attrs={"rows": 3})
    )
    manufacturer = forms.CharField(label=_("Manufacturer"), max_length=100, required=False)
    manufacturer_ref = forms.CharField(
        label=_("Manufacturer reference"), max_length=100, required=False
    )
    ean = forms.RegexField(
        label=_("EAN / barcode"),
        regex=EAN_PATTERN,
        max_length=14,
        required=False,
        error_messages={"invalid": _("Use 8 to 14 digits.")},
    )
    is_stock_item = forms.BooleanField(
        label=_("Stock item"),
        required=False,
        help_text=_("Kept in the warehouse, so it can be handed over from stock."),
    )

    def __init__(self, *args: Any, ctx: AccessContext, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["unit"].queryset = selectors.units(ctx)  # type: ignore[attr-defined]
        self.fields["category"].queryset = selectors.categories(ctx)  # type: ignore[attr-defined]

    def values(self) -> dict[str, Any]:
        data = self.cleaned_data
        category, unit = data.get("category"), data["unit"]
        return {
            "name": data["name"],
            "unit_id": unit.pk,
            "category_id": category.pk if category is not None else None,
            "description": data["description"],
            "manufacturer": data["manufacturer"],
            "manufacturer_ref": data["manufacturer_ref"],
            "ean": data["ean"] or None,
            "is_stock_item": data["is_stock_item"],
        }

    @staticmethod
    def initial_for(material: Material) -> dict[str, Any]:
        return {
            "name": material.name,
            "unit": material.unit_id,
            "category": material.category_id,
            "description": material.description,
            "manufacturer": material.manufacturer,
            "manufacturer_ref": material.manufacturer_ref,
            "ean": material.ean,
            "is_stock_item": material.is_stock_item,
        }


class MaterialCreateForm(IdempotentForm, MaterialForm):
    operation = "material.create"

    reference = forms.CharField(
        label=_("Reference"),
        max_length=60,
        help_text=_("Your own code for the material. It cannot change later."),
    )
    field_order = ["reference", "name"]

    def create_kwargs(self) -> dict[str, Any]:
        return {
            "reference": self.cleaned_data["reference"],
            **self.values(),
            "idempotency_key": self.cleaned_data["idempotency_key"],
        }


class CategoryForm(forms.Form):
    name = forms.CharField(label=_("Name"), max_length=100)
    parent = CategoryChoiceField(
        label=_("Parent category"),
        queryset=None,
        required=False,
        help_text=_("Categories have two levels at most."),
    )

    def __init__(
        self, *args: Any, ctx: AccessContext, editing: MaterialCategory | None = None, **kwargs: Any
    ):
        super().__init__(*args, **kwargs)
        roots = selectors.root_categories(ctx)
        if editing is not None:
            roots = roots.exclude(pk=editing.pk)
        self.fields["parent"].queryset = roots  # type: ignore[attr-defined]

    def values(self) -> dict[str, Any]:
        parent = self.cleaned_data["parent"]
        return {
            "name": self.cleaned_data["name"],
            "parent_id": parent.pk if parent is not None else None,
        }

    @staticmethod
    def initial_for(category: MaterialCategory) -> dict[str, Any]:
        return {"name": category.name, "parent": category.parent_id}


class CategoryCreateForm(IdempotentForm, CategoryForm):
    operation = "category.create"

    def create_kwargs(self) -> dict[str, Any]:
        return {**self.values(), "idempotency_key": self.cleaned_data["idempotency_key"]}
