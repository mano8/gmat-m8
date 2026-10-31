"""Admin for the catalog (plan §5, "Django Admin").

Materials and categories are master data: editable by whoever holds ``catalog.manage``
who is also admitted to the admin site (group administrators), saved through the services
(audit, normalization, constraints). Units of measure are seeded and read-only.
"""

from typing import Any

from django.contrib import admin
from django.http import HttpRequest

from apps.access.admin import CapabilityModelAdmin
from apps.access.principals import SessionPrincipal
from apps.access.roles import Capability
from apps.catalog import services
from apps.catalog.models import Material, MaterialCategory, UnitOfMeasure
from apps.core.admin import ReadOnlyModelAdmin


class CatalogDataAdmin(CapabilityModelAdmin):
    edit_capability = Capability.CATALOG_MANAGE


@admin.register(MaterialCategory)
class MaterialCategoryAdmin(CatalogDataAdmin):
    list_display = ("name", "parent")
    list_select_related = ("parent",)
    search_fields = ("name",)
    fields = ("name", "parent")

    def save_via_service(self, request: HttpRequest, obj: Any, form: Any, change: bool) -> None:
        principal = SessionPrincipal.from_request(request)
        if change:
            saved = services.update_category(principal, obj.pk, **self.form_values(form))
        else:
            saved = services.create_category(
                principal, **self.form_values(form, ("name", "parent"))
            )
        self.adopt(obj, saved)


@admin.register(Material)
class MaterialAdmin(CatalogDataAdmin):
    list_display = ("reference", "name", "category", "unit", "is_stock_item", "is_active")
    list_filter = ("is_active", "is_stock_item", "unit")
    list_select_related = ("category", "unit")
    search_fields = ("reference", "name", "manufacturer_ref", "ean")

    FIELDS = (
        "name",
        "unit",
        "category",
        "description",
        "manufacturer",
        "manufacturer_ref",
        "ean",
        "is_stock_item",
    )

    def get_fields(self, request: HttpRequest, obj: Any = None) -> tuple[str, ...]:
        return (
            ("reference", *self.FIELDS) if obj is None else ("reference", *self.FIELDS, "is_active")
        )

    def get_readonly_fields(self, request: HttpRequest, obj: Any = None) -> tuple[str, ...]:
        # Activation goes through the deactivate / reactivate services (the in-app buttons).
        return () if obj is None else ("reference", "is_active")

    def save_via_service(self, request: HttpRequest, obj: Any, form: Any, change: bool) -> None:
        principal = SessionPrincipal.from_request(request)
        if change:
            saved = services.update_material(principal, obj.pk, **self.form_values(form))
        else:
            saved = services.create_material(
                principal, **self.form_values(form, ("reference", *self.FIELDS))
            )
        self.adopt(obj, saved)


@admin.register(UnitOfMeasure)
class UnitOfMeasureAdmin(ReadOnlyModelAdmin):
    list_display = ("code", "name", "decimals")
    search_fields = ("code", "name")
