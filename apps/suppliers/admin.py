"""Admin for suppliers: read-only (ADR-0030); they are managed on ``/suppliers/``."""

from django.contrib import admin

from apps.core.admin import ReadOnlyModelAdmin
from apps.suppliers.models import Supplier


@admin.register(Supplier)
class SupplierAdmin(ReadOnlyModelAdmin):
    list_display = ("name", "tax_id", "contact_name", "email", "phone", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name", "tax_id", "contact_name", "email")
    fields = (
        "name",
        "tax_id",
        "contact_name",
        "email",
        "phone",
        "address",
        "notes",
        "is_active",
    )
