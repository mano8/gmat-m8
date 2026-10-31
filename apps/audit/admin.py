"""Read-only admin for the audit trail (plan §5, "Django Admin")."""

from django.contrib import admin

from apps.audit.models import AuditEvent
from apps.core.admin import ReadOnlyModelAdmin


@admin.register(AuditEvent)
class AuditEventAdmin(ReadOnlyModelAdmin):
    list_display = ("occurred_at", "action", "actor_label", "via", "object_type", "object_id")
    list_filter = ("action",)
    search_fields = ("actor_label", "action")
    date_hierarchy = "occurred_at"
