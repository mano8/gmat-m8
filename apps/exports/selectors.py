"""The rows of the CSV exports (plan M7.3, ``export.csv``).

Each export is a header and the rows of the lines in the user's ``export.csv`` scope, one row per
document line, oldest first. Orders are read from what they were issued with (the buyer, supplier
and destination snapshots and the lines' material snapshots; a draft has none and is left out), so
a later change to the catalog or to a supplier does not rewrite a past order. Prices are a column
only for who holds ``price.view`` somewhere and a value only on the orders where they hold it.
Requests that are still drafts belong to their author alone and are never exported.
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from django.db.models import Exists, OuterRef, QuerySet
from django.utils.translation import gettext_lazy as _

from apps.access.context import AccessContext
from apps.access.roles import Capability
from apps.catalog.models import Material
from apps.exports.cells import cell
from apps.material_requests.models import MaterialRequest, MaterialRequestLine
from apps.purchasing.models import PurchaseOrder, PurchaseOrderLine
from apps.receiving.models import GoodsReceiptLine

EXPORT = Capability.EXPORT_CSV
PRICE = Capability.PRICE_VIEW
CHUNK = 500


@dataclass(frozen=True)
class Export:
    """A named export: its file name and the function that builds ``(header, rows)``."""

    key: str
    title: Any
    build: Callable[[AccessContext], tuple[list[str], Iterator[list[Any]]]]


def can_export(ctx: AccessContext) -> bool:
    return ctx.has(EXPORT)


def _rows(queryset: QuerySet[Any], row: Callable[[Any], list[Any]]) -> Iterator[list[Any]]:
    for item in queryset.iterator(chunk_size=CHUNK):
        yield [cell(value) for value in row(item)]


# --- requests ----------------------------------------------------------------------------

REQUEST_HEADER = [
    "request",
    "status",
    "priority",
    "needed_by",
    "project_code",
    "project_name",
    "procurement_branch",
    "requested_by",
    "submitted_at",
    "line",
    "material_reference",
    "description",
    "unit",
    "requested_qty",
    "approved_qty",
    "decision",
    "sourcing",
    "closed_short",
    "close_reason",
    "notes",
]


def _request_row(line: MaterialRequestLine) -> list[Any]:
    request = line.request
    material = line.material
    return [
        request.number,
        request.status,
        request.priority,
        request.needed_by,
        request.project.code,
        request.project.name,
        request.procurement_branch.code,
        request.requested_by.email,
        request.submitted_at,
        line.line_no,
        material.reference if material is not None else "",
        material.name if material is not None else line.free_text,
        line.unit.code,
        line.requested_qty,
        line.approved_qty,
        line.decision,
        line.sourcing,
        line.closed_short_at,
        line.close_reason,
        request.notes,
    ]


def requests(ctx: AccessContext) -> tuple[list[str], Iterator[list[Any]]]:
    lines = (
        MaterialRequestLine.objects.visible_to(ctx, EXPORT)
        .exclude(request__status=MaterialRequest.Status.DRAFT)
        .select_related(
            "request__project",
            "request__procurement_branch",
            "request__requested_by",
            "unit",
            "material",
        )
        .order_by("request__submitted_at", "request__number", "line_no")
    )
    return REQUEST_HEADER, _rows(lines, _request_row)


# --- purchase orders ---------------------------------------------------------------------

ORDER_HEADER = [
    "order",
    "revision",
    "status",
    "issued_at",
    "buyer_company",
    "buyer_branch",
    "supplier",
    "supplier_tax_id",
    "ship_to",
    "ship_to_project",
    "expected_date",
    "supplier_reference",
    "line",
    "material_reference",
    "material_name",
    "unit",
    "ordered_qty",
    "closed_short",
    "close_reason",
]
PRICE_HEADER = ["unit_price", "discount_pct", "currency"]


def _live(material: Material) -> dict[str, str]:
    """A line without a snapshot (written outside the services): the catalog as it is."""
    return {"reference": material.reference, "name": material.name}


def _order_row(line: PurchaseOrderLine, *, with_prices: bool) -> list[Any]:
    order = line.po
    buyer, supplier = order.buyer_snapshot, order.supplier_snapshot
    ship_to = order.ship_to_snapshot
    material = line.material_snapshot or _live(line.material)
    row = [
        order.number,
        order.revision,
        order.status,
        order.issued_at,
        (buyer.get("company") or {}).get("code", ""),
        (buyer.get("branch") or {}).get("code", ""),
        supplier.get("name", ""),
        supplier.get("tax_id", ""),
        ship_to.get("name", ""),
        (ship_to.get("project") or {}).get("code", ""),
        order.expected_date,
        order.supplier_ref,
        line.line_no,
        material.get("reference", ""),
        material.get("name", ""),
        line.unit.code,
        line.ordered_qty,
        line.closed_short_at,
        line.close_reason,
    ]
    if with_prices:
        priced = line.priced  # type: ignore[attr-defined]
        row += [
            line.unit_price if priced else "",
            line.discount_pct if priced else "",
            order.currency if priced else "",
        ]
    return row


def orders(ctx: AccessContext) -> tuple[list[str], Iterator[list[Any]]]:
    with_prices = ctx.has(PRICE)
    priced = PurchaseOrder.objects.visible_to(ctx, PRICE).filter(pk=OuterRef("po_id"))
    lines = (
        PurchaseOrderLine.objects.visible_to(ctx, EXPORT)
        .exclude(po__status=PurchaseOrder.Status.DRAFT)
        .annotate(priced=Exists(priced))
        .select_related("po", "unit", "material")
        .order_by("po__issued_at", "po__number", "line_no")
    )
    header = ORDER_HEADER + (PRICE_HEADER if with_prices else [])
    return header, _rows(lines, lambda line: _order_row(line, with_prices=with_prices))


# --- receipts ----------------------------------------------------------------------------

RECEIPT_HEADER = [
    "receipt",
    "status",
    "order",
    "supplier",
    "received_at",
    "location",
    "delivery_note",
    "line",
    "material_reference",
    "material_name",
    "accepted_qty",
    "rejected_qty",
    "incident",
    "incident_note",
    "surplus_qty",
    "voided_at",
    "void_reason",
]


def _receipt_row(line: GoodsReceiptLine) -> list[Any]:
    receipt, order = line.receipt, line.po
    material = line.po_line.material_snapshot or _live(line.po_line.material)
    return [
        receipt.number,
        receipt.status,
        order.number,
        order.supplier_snapshot.get("name", ""),
        receipt.received_at,
        receipt.location.name,
        receipt.delivery_note_ref,
        line.po_line.line_no,
        material.get("reference", ""),
        material.get("name", ""),
        line.qty_received,
        line.qty_rejected,
        line.issue,
        line.issue_note,
        line.surplus_qty,
        receipt.voided_at,
        receipt.void_reason,
    ]


def receipts(ctx: AccessContext) -> tuple[list[str], Iterator[list[Any]]]:
    lines = (
        GoodsReceiptLine.objects.visible_to(ctx, EXPORT)
        .select_related("receipt__location", "po", "po_line__material")
        .order_by("receipt__received_at", "receipt__number", "po_line__line_no")
    )
    return RECEIPT_HEADER, _rows(lines, _receipt_row)


EXPORTS = (
    Export("requests", _("Material requests"), requests),
    Export("orders", _("Purchase orders"), orders),
    Export("receipts", _("Goods receipts"), receipts),
)
BY_KEY = {export.key: export for export in EXPORTS}
