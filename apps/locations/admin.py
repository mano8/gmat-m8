"""Admin for locations: warehouses are master data (plan §5, "Django Admin").

Worksites are created with their project; here they can be renamed or deactivated.
"""

from typing import Any

from django.contrib import admin
from django.http import HttpRequest

from apps.access.principals import SessionPrincipal
from apps.locations import services
from apps.locations.models import Location
from apps.organizations.admin import MasterDataAdmin
from apps.organizations.models import Branch


@admin.register(Location)
class LocationAdmin(MasterDataAdmin):
    list_display = ("name", "kind", "branch", "project", "is_active")
    list_filter = ("kind", "is_active")
    list_select_related = ("branch__company", "project")
    search_fields = ("name", "branch__code", "project__code")

    def get_fields(self, request: HttpRequest, obj: Any = None) -> tuple[str, ...]:
        if obj is None:
            return ("branch", "name", "address")
        return ("kind", "branch", "project", "name", "address", "is_active")

    def get_readonly_fields(self, request: HttpRequest, obj: Any = None) -> tuple[str, ...]:
        return () if obj is None else ("kind", "branch", "project")

    def formfield_for_foreignkey(self, db_field: Any, request: HttpRequest, **kwargs: Any) -> Any:
        if db_field.name == "branch":
            kwargs["queryset"] = Branch.objects.filter(is_active=True).select_related("company")
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def save_via_service(self, request: HttpRequest, obj: Any, form: Any, change: bool) -> None:
        principal = SessionPrincipal.from_request(request)
        if change:
            saved = services.update_location(principal, obj.pk, **self.form_values(form))
        else:
            values = self.form_values(form, ("branch", "name", "address"))
            saved = services.create_warehouse(principal, **values)
        self.adopt(obj, saved)
