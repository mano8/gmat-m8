"""Admin for the catalog: read-only (ADR-0030).

Materials and categories are managed on ``/catalog/``, units of measure on
``/catalog/units/``.
"""

from django.contrib import admin

from apps.catalog.models import Material, MaterialCategory, UnitOfMeasure
from apps.core.admin import ReadOnlyModelAdmin


@admin.register(MaterialCategory)
class MaterialCategoryAdmin(ReadOnlyModelAdmin):
    list_display = ("name", "parent")
    list_select_related = ("parent",)
    search_fields = ("name",)
    fields = ("name", "parent")


@admin.register(Material)
class MaterialAdmin(ReadOnlyModelAdmin):
    list_display = ("reference", "name", "category", "unit", "is_stock_item", "is_active")
    list_filter = ("is_active", "is_stock_item", "unit")
    list_select_related = ("category", "unit")
    search_fields = ("reference", "name", "manufacturer_ref", "ean")
    fields = (
        "reference",
        "name",
        "unit",
        "category",
        "description",
        "manufacturer",
        "manufacturer_ref",
        "ean",
        "is_stock_item",
        "is_active",
    )


@admin.register(UnitOfMeasure)
class UnitOfMeasureAdmin(ReadOnlyModelAdmin):
    list_display = ("code", "name", "decimals", "is_active")
    list_filter = ("is_active",)
    search_fields = ("code", "name")
