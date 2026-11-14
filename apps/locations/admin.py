"""Admin for locations: read-only (ADR-0030).

Warehouses are managed on ``/org/warehouses/``; a worksite comes with its project and is
renamed or deactivated from the project's page.
"""

from django.contrib import admin

from apps.core.admin import ReadOnlyModelAdmin
from apps.locations.models import Location


@admin.register(Location)
class LocationAdmin(ReadOnlyModelAdmin):
    list_display = ("name", "kind", "branch", "project", "is_active")
    list_filter = ("kind", "is_active")
    list_select_related = ("branch__company", "project")
    search_fields = ("name", "branch__code", "project__code")
    fields = ("kind", "branch", "project", "name", "address", "is_active")
