"""Read-only admin for purchase orders (plan §5, "Django Admin").

Orders are documents: they change only through their services, from the app. Prices are
left out: the admin admits platform operators, who hold no ``price.view`` (plan §3.4).
"""

from django.contrib import admin

from apps.core.admin import ReadOnlyModelAdmin
from apps.purchasing.models import PurchaseAllocation, PurchaseOrder, PurchaseOrderLine


@admin.register(PurchaseOrder)
class PurchaseOrderAdmin(ReadOnlyModelAdmin):
    list_display = ("number", "branch", "supplier", "status", "revision", "expected_date")
    list_filter = ("status",)
    list_select_related = ("branch__company", "supplier")
    search_fields = ("number", "supplier__name", "supplier_ref")


@admin.register(PurchaseOrderLine)
class PurchaseOrderLineAdmin(ReadOnlyModelAdmin):
    exclude = ("unit_price", "discount_pct")
    list_display = ("po", "line_no", "material", "unit", "ordered_qty", "closed_short_at")
    list_select_related = ("po", "material", "unit")
    search_fields = ("po__number", "material__reference")


@admin.register(PurchaseAllocation)
class PurchaseAllocationAdmin(ReadOnlyModelAdmin):
    list_display = ("po_line", "request_line", "qty")
    list_select_related = ("po_line__po", "request_line__request")
    search_fields = ("po_line__po__number", "request_line__request__number")
