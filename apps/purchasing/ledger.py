"""Purchasing's side of the ledger, read from the database (plan §4.5, S9).

- ``book_for_*``: load order lines, request lines and the allocations between them into a
  ``waterfall.Book`` (received and attributed come from ``receipts``);
- ``request_line_flows``: the ``FlowSource`` purchasing registers with
  ``material_requests.ledger``: received R (attributed over every order), pending P, draft
  reservations Q and the allocated ceiling on draft or issued orders;
- ``issued_orders``: the deactivation provider; an issued order blocks its branch and the
  branch of the location it ships to (S9, F62).

A waterfall needs every allocation of its order line, so loading a request line's figures
reads the other allocations of the order lines it is on.
"""

from collections.abc import Collection, Iterable
from uuid import UUID

from django.db.models import BooleanField, ExpressionWrapper, Q

from apps.material_requests.ledger import Flows, open_q
from apps.material_requests.models import MaterialRequestLine
from apps.organizations.deactivation import OpenDocument
from apps.purchasing import receipts
from apps.purchasing.models import PurchaseAllocation, PurchaseOrder, PurchaseOrderLine
from apps.purchasing.waterfall import (
    ZERO,
    AllocationFacts,
    Book,
    OrderLineFacts,
    RequestLineFacts,
)

# Allocations that connect lines: a positive ceiling on an order that is not cancelled.
LIVE = Q(qty__gt=0) & ~Q(po_line__po__status=PurchaseOrder.Status.CANCELLED)
_OPEN = ExpressionWrapper(open_q(), output_field=BooleanField())


def _request_lines(ids: Collection[UUID]) -> list[RequestLineFacts]:
    rows = (
        MaterialRequestLine.objects.filter(pk__in=list(ids))
        .annotate(is_open=_OPEN)
        .values_list(
            "pk",
            "request_id",
            "approved_qty",
            "is_open",
            "request__priority",
            "request__needed_by",
            "request__submitted_at",
        )
    )
    return [
        RequestLineFacts(
            id=pk,
            request_id=request_id,
            approved=approved or ZERO,
            is_open=is_open,
            priority=priority,
            needed_by=needed_by,
            submitted_at=submitted_at,
        )
        for pk, request_id, approved, is_open, priority, needed_by, submitted_at in rows
    ]


def _order_lines(ids: Collection[UUID]) -> list[OrderLineFacts]:
    rows = PurchaseOrderLine.objects.filter(pk__in=list(ids)).values_list(
        "pk", "po_id", "po__status", "ordered_qty", "closed_short_at"
    )
    found = receipts.line_receipts([row[0] for row in rows])
    return [
        OrderLineFacts(
            id=pk,
            po_id=po_id,
            order_status=status,
            ordered=ordered,
            received=found[pk].received,
            closed_short=closed is not None,
        )
        for pk, po_id, status, ordered, closed in rows
    ]


def _book(
    order_line_ids: Iterable[UUID],
    request_line_ids: Iterable[UUID],
    rows: Iterable[tuple[UUID, UUID, UUID, object]],
) -> Book:
    listed = list(rows)
    attributed = receipts.attributed([row[0] for row in listed])
    allocations = [
        AllocationFacts(pk, po_line_id, request_line_id, qty, attributed[pk])  # type: ignore[arg-type]
        for pk, po_line_id, request_line_id, qty in listed
    ]
    order_lines = set(order_line_ids) | {a.po_line_id for a in allocations}
    request_lines = set(request_line_ids) | {a.request_line_id for a in allocations}
    return Book(_order_lines(order_lines), _request_lines(request_lines), allocations)


_ROW = ("pk", "po_line_id", "request_line_id", "qty")


def book_for_nodes(order_line_ids: Collection[UUID], request_line_ids: Collection[UUID]) -> Book:
    """These lines and the live allocations of any of them (a component's rows)."""
    rows = (
        PurchaseAllocation.objects.filter(LIVE)
        .filter(
            Q(po_line_id__in=list(order_line_ids)) | Q(request_line_id__in=list(request_line_ids))
        )
        .values_list(*_ROW)
    )
    return _book(order_line_ids, request_line_ids, rows)


def book_for_order_lines(order_line_ids: Collection[UUID], *, empty: bool = False) -> Book:
    """These order lines with their allocations (``empty``: also those trimmed to zero)."""
    allocations = PurchaseAllocation.objects.filter(po_line_id__in=list(order_line_ids))
    if not empty:
        allocations = allocations.filter(qty__gt=0)
    return _book(order_line_ids, (), allocations.values_list(*_ROW))


def book_for_request_lines(request_line_ids: Collection[UUID]) -> Book:
    """These request lines and every waterfall they take part in."""
    order_lines = set(
        PurchaseAllocation.objects.filter(
            LIVE, request_line_id__in=list(request_line_ids)
        ).values_list("po_line_id", flat=True)
    )
    rows = PurchaseAllocation.objects.filter(LIVE, po_line_id__in=order_lines).values_list(*_ROW)
    return _book(order_lines, request_line_ids, rows)


def request_line_flows(line_ids: Collection[UUID]) -> dict[UUID, Flows]:
    """R, P, Q and the allocated ceiling of these request lines (a ``FlowSource``)."""
    if not line_ids:
        return {}
    book = book_for_request_lines(line_ids)
    flows: dict[UUID, Flows] = {}
    for line_id in line_ids:
        if book.of_request_line(line_id):
            totals = book.request_line_totals(line_id)
            flows[line_id] = Flows(
                received=totals.received,
                pending=totals.pending,
                reserved=totals.reserved,
                allocated=totals.allocated,
            )
    return flows


def issued_orders(branch_ids: Collection[UUID]) -> list[OpenDocument]:
    """Issued orders of these branches, or shipping to one of their locations (S9, F62)."""
    rows = PurchaseOrder.objects.filter(status=PurchaseOrder.Status.ISSUED).filter(
        Q(branch_id__in=list(branch_ids)) | Q(ship_to__branch_id__in=list(branch_ids))
    )
    return sorted(
        OpenDocument("purchase_order", str(number), pk)
        for number, pk in rows.values_list("number", "pk")
    )
