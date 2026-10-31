"""Read-only admin for material requests (plan §5, "Django Admin").

Requests are documents: they change only through their services, from the app.
"""

from django.contrib import admin

from apps.core.admin import ReadOnlyModelAdmin
from apps.material_requests.models import MaterialRequest, MaterialRequestLine


@admin.register(MaterialRequest)
class MaterialRequestAdmin(ReadOnlyModelAdmin):
    list_display = ("number", "project", "procurement_branch", "requested_by", "status")
    list_filter = ("status", "priority")
    list_select_related = ("project", "procurement_branch__company", "requested_by")
    search_fields = ("number", "project__code", "requested_by__email")


@admin.register(MaterialRequestLine)
class MaterialRequestLineAdmin(ReadOnlyModelAdmin):
    list_display = ("request", "line_no", "material", "free_text", "unit", "requested_qty")
    list_filter = ("decision", "sourcing")
    list_select_related = ("request", "material", "unit")
    search_fields = ("request__number", "material__reference", "free_text")
