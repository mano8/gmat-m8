"""Reads of receiving (plan §3.4, §4.5): every row through ``visible_to``.

``expected_deliveries`` is what is still coming to one project, for whoever may receive it
there (``receipt.view``): the project's site manager first of all (○ site, Q9), and the
warehouse operators and material managers of the branch that buys for it. Per allocation of
that project on an issued order whose line can still receive, it shows only the fields of
§3.4: order number, supplier display name and expected date, material reference and name,
unit, and the quantity still expected **for that project**. Never a price, the supplier's
contact details, another project's quantities or the rest of the order.

The quantity expected is what a site receipt could still attribute to the allocation: its
ceiling remaining, capped by what the order line can still receive and by its request
line's receipt capacity A - R and outstanding A - D (§4.5).
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID

from apps.access.context import AccessContext
from apps.access.exceptions import NotInScope
from apps.access.roles import Capability
from apps.material_requests import ledger as request_ledger
from apps.material_requests.models import MaterialRequestLine
from apps.projects.models import Project
from apps.purchasing.ledger import book_for_order_lines
from apps.purchasing.models import PurchaseAllocation, PurchaseOrder
from apps.purchasing.waterfall import Book

VIEW = Capability.RECEIPT_VIEW
ZERO = Decimal("0")


@dataclass(frozen=True)
class ExpectedDelivery:
    """One allocation of the project still expecting goods (§3.4's projection)."""

    po_id: UUID
    po_line_id: UUID
    allocation_id: UUID
    order_number: str
    supplier: str
    expected_date: date | None
    material_reference: str
    material_name: str
    unit: str
    request_number: str
    expected: Decimal


def _expected(allocation: PurchaseAllocation, book: Book, flow: request_ledger.Flows) -> Decimal:
    """What a site receipt could still attribute to ``allocation`` (module docstring)."""
    order_line = book.order_lines[allocation.po_line_id]
    approved = allocation.request_line.approved_qty or ZERO
    return min(
        book.allocations[allocation.pk].ceiling_remaining,
        order_line.remaining if order_line.receivable else ZERO,
        approved - flow.received,
        approved - flow.delivered,
    )


def _row(allocation: PurchaseAllocation, expected: Decimal) -> ExpectedDelivery:
    po_line = allocation.po_line
    order = po_line.po
    return ExpectedDelivery(
        po_id=order.pk,
        po_line_id=po_line.pk,
        allocation_id=allocation.pk,
        order_number=order.number or "",
        supplier=str(order.supplier_snapshot.get("name", "")),
        expected_date=order.expected_date,
        material_reference=str(po_line.material_snapshot.get("reference", "")),
        material_name=str(po_line.material_snapshot.get("name", "")),
        unit=po_line.unit.code,
        request_number=allocation.request_line.request.number or "",
        expected=expected,
    )


def _soonest_first(row: ExpectedDelivery) -> tuple[object, ...]:
    return (
        row.expected_date is None,
        row.expected_date or date.min,
        row.order_number,
        row.material_reference,
        row.request_number,
    )


def expected_deliveries(ctx: AccessContext, project_id: UUID) -> list[ExpectedDelivery]:
    """What is still coming to this project, soonest first (404 outside ``receipt.view``)."""
    project = Project.objects.visible_to(ctx, VIEW).filter(pk=project_id).first()
    if project is None:
        raise NotInScope(VIEW)
    lines = (
        MaterialRequestLine.objects.visible_to(ctx, VIEW)
        .filter(request_ledger.open_q(), project=project)
        .values("pk")
    )
    allocations = list(
        PurchaseAllocation.objects.filter(
            request_line__in=lines, qty__gt=0, po_line__po__status=PurchaseOrder.Status.ISSUED
        ).select_related("po_line__po", "po_line__unit", "request_line__request")
    )
    if not allocations:
        return []
    book = book_for_order_lines({allocation.po_line_id for allocation in allocations})
    flows = request_ledger.flows({allocation.request_line_id for allocation in allocations})
    expected = {
        allocation.pk: _expected(allocation, book, flows[allocation.request_line_id])
        for allocation in allocations
    }
    rows = [_row(a, expected[a.pk]) for a in allocations if expected[a.pk] > 0]
    return sorted(rows, key=_soonest_first)
