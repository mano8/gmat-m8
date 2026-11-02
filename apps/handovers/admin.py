"""Read-only admin for handovers (plan §5, "Django Admin").

Handovers are documents: they are posted and voided only through their services, from the
app (a direct one only with its receipt), and never change except by their void (I11).
"""

from django.contrib import admin

from apps.core.admin import ReadOnlyModelAdmin
from apps.handovers.models import Handover, HandoverLine


@admin.register(Handover)
class HandoverAdmin(ReadOnlyModelAdmin):
    list_display = ("number", "kind", "project", "branch", "handed_at", "status")
    list_filter = ("kind", "status")
    list_select_related = ("project", "branch__company")
    search_fields = ("number", "project__code", "goods_receipt__number")


@admin.register(HandoverLine)
class HandoverLineAdmin(ReadOnlyModelAdmin):
    list_display = ("handover", "request_line", "qty")
    list_select_related = ("handover", "request_line__request")
    search_fields = ("handover__number", "request_line__request__number")
