"""Admin for suppliers (plan §5, "Django Admin"): master data, saved through the services.

Editable by whoever holds ``supplier.manage`` and is admitted to the admin site (group
administrators); activation goes through the deactivate / reactivate buttons of the app.
"""

from typing import Any

from django.contrib import admin
from django.http import HttpRequest

from apps.access.admin import CapabilityModelAdmin
from apps.access.principals import SessionPrincipal
from apps.access.roles import Capability
from apps.suppliers import services
from apps.suppliers.models import Supplier


@admin.register(Supplier)
class SupplierAdmin(CapabilityModelAdmin):
    edit_capability = Capability.SUPPLIER_MANAGE

    list_display = ("name", "tax_id", "contact_name", "email", "phone", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name", "tax_id", "contact_name", "email")

    FIELDS = ("name", "tax_id", "contact_name", "email", "phone", "address", "notes")

    def get_fields(self, request: HttpRequest, obj: Any = None) -> tuple[str, ...]:
        return self.FIELDS if obj is None else (*self.FIELDS, "is_active")

    def get_readonly_fields(self, request: HttpRequest, obj: Any = None) -> tuple[str, ...]:
        return () if obj is None else ("is_active",)

    def save_via_service(self, request: HttpRequest, obj: Any, form: Any, change: bool) -> None:
        principal = SessionPrincipal.from_request(request)
        if change:
            saved = services.update_supplier(principal, obj.pk, **self.form_values(form))
        else:
            saved = services.create_supplier(principal, **self.form_values(form, self.FIELDS))
        self.adopt(obj, saved)
