"""Read-only admin for the audit trail (plan §5, "Django Admin").

Like the in-app viewer (``apps.audit.selectors``), it never shows an event's payload, which
may hold prices (an order line's price changes), or the client address.
"""

from django.contrib import admin

from apps.audit.models import AuditEvent
from apps.core.admin import ReadOnlyModelAdmin


@admin.register(AuditEvent)
class AuditEventAdmin(ReadOnlyModelAdmin):
    list_display = ("occurred_at", "action", "actor_label", "via", "object_type", "object_id")
    list_filter = ("action",)
    search_fields = ("actor_label", "action")
    date_hierarchy = "occurred_at"
    exclude = ("data", "ip")
