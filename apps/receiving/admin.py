"""Read-only admin for goods receipts (plan §5, "Django Admin").

Receipts are documents: they are posted and voided only through their services, from the
app, and a posted receipt never changes except by its void (I11).
"""

from django.contrib import admin

from apps.core.admin import ReadOnlyModelAdmin
from apps.receiving.models import GoodsReceipt, GoodsReceiptLine, ReceiptAttribution


@admin.register(GoodsReceipt)
class GoodsReceiptAdmin(ReadOnlyModelAdmin):
    list_display = ("number", "po", "branch", "location", "received_at", "status")
    list_filter = ("status",)
    list_select_related = ("po", "branch__company", "location")
    search_fields = ("number", "po__number", "delivery_note_ref")


@admin.register(GoodsReceiptLine)
class GoodsReceiptLineAdmin(ReadOnlyModelAdmin):
    list_display = ("receipt", "po_line", "qty_received", "qty_rejected", "issue", "surplus_qty")
    list_filter = ("issue",)
    list_select_related = ("receipt", "po_line__po")
    search_fields = ("receipt__number", "po_line__po__number")


@admin.register(ReceiptAttribution)
class ReceiptAttributionAdmin(ReadOnlyModelAdmin):
    list_display = ("receipt_line", "allocation", "qty")
    list_select_related = ("receipt_line__receipt", "allocation__request_line__request")
    search_fields = ("receipt_line__receipt__number", "allocation__request_line__request__number")
