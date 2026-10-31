"""Issuing, amending, cancelling and closing purchase orders (plan §4.3, §4.4, §4.9).

- ``issue``: number per buyer company, snapshots, revalidation (F38): a draft whose lines
  would leave a request line committed above its approved quantity is refused, with the
  list of lines, before any trim;
- ``amend``: an issued order's changes of §4.4, one revision per call;
- ``cancel``: a draft is discarded, an issued order without posted receipts cancelled;
  every allocation is trimmed to zero and unsent dispatches are cancelled (§5);
- ``close_line_short``: an order line gives up the rest; its allocations are trimmed to
  what is attributed and the order closes once no line has capacity (§4.9).
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from types import EllipsisType
from typing import Any
from uuid import UUID

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.access.principals import Principal
from apps.catalog.quantities import check_quantity
from apps.core.changes import apply_changes
from apps.core.db import db_now
from apps.core.idempotency import execute_once
from apps.core.locking import LockOrder, LockSetChanged, transactional
from apps.material_requests.models import MaterialRequestLine
from apps.purchasing import receipts
from apps.purchasing.components import Component, TrimReason, over_committed
from apps.purchasing.dispatch import cancel_dispatches, lock_dispatches
from apps.purchasing.models import PurchaseAllocation, PurchaseOrder, PurchaseOrderLine
from apps.purchasing.services.common import (
    RequestItem,
    Status,
    _active_supplier,
    _add_items,
    _assign_number,
    _authorize,
    _check_prices,
    _check_ship_to,
    _check_worksite_projects,
    _increase_allocation,
    _lock_order,
    _material_snapshot,
    _order,
    _order_snapshots,
    _quantity,
    _reason,
    _record,
    _reduce_ordered,
    _requests,
    _require_active_branch,
    _require_issued,
    _save_line,
    _settle_order,
    _status_error,
)
from apps.purchasing.transitions import transition
from apps.purchasing.waterfall import ISSUED

# --- issue ----------------------------------------------------------------------------


def _revalidate(component: Component, order: PurchaseOrder, lines: list[PurchaseOrderLine]) -> None:
    """I3 and C6 on every line, and I2 on the component as if the order were issued."""
    book = component.book()
    for line in lines:
        if book.order_line_totals(line.pk).allocated > line.ordered_qty:
            raise ValidationError(
                _("Line %(line)s allocates more than it orders."),
                code="over_allocated",
                params={"line": line.line_no},
            )
    _check_worksite_projects(order, order.ship_to)
    issued = book.with_order_status(order.pk, ISSUED)
    delivered = component.delivered(issued)
    over = over_committed(issued, delivered)
    if not over:
        return
    found = MaterialRequestLine.objects.select_related("request").in_bulk(
        [line.id for line in over]
    )
    raise ValidationError(
        [
            ValidationError(
                _("%(request)s line %(line)s would be committed %(committed)s of %(approved)s."),
                code="over_committed",
                params={
                    "request": found[line.id].request,
                    "line": found[line.id].line_no,
                    "committed": _quantity(
                        issued.committed(line.id, delivered.get(line.id, Decimal(0)))
                    ),
                    "approved": _quantity(line.approved),
                },
            )
            for line in over
        ]
    )


@transactional
def issue(
    principal: Principal, po_id: UUID, *, idempotency_key: str | None = None
) -> PurchaseOrder:
    """Send a draft to the supplier: number, snapshots, revalidation (F38).

    Its reservations become pending. With an idempotency key, a repeated issue (a double
    tap) returns the order again.
    """
    locks = LockOrder()
    order = _order(po_id)
    actor = _authorize(principal, order, locks)

    def execute() -> PurchaseOrder:
        locked, component = _lock_order(locks, order)
        transition(locked, Status.ISSUED, "issue")
        lines = list(locked.lines.select_related("material"))
        if not lines:
            raise ValidationError(_("Add at least one line before issuing."), code="empty")
        _require_active_branch(locked.branch)
        _active_supplier(locked.supplier_id)
        _check_ship_to(locked.branch, locked.ship_to)
        _revalidate(component, locked, lines)
        for line in lines:
            line.material_snapshot = _material_snapshot(line.material)
            line.save(update_fields=["material_snapshot", "updated_at"])
        for name, snapshot in _order_snapshots(locked).items():
            setattr(locked, name, snapshot)
        locked.issued_at, locked.issued_by = db_now(), actor
        _assign_number(locked, locks)
        locked.save()
        _record(principal, "purchase_order.issued", locked, lines=len(lines))
        return locked

    return execute_once(
        actor_id=principal.user_id,
        key=idempotency_key,
        operation="purchase_order.issue",
        payload={"po_id": po_id},
        execute=execute,
        replay=lambda _type, pk: PurchaseOrder.objects.get(pk=pk),
    )


# --- amend ----------------------------------------------------------------------------


@dataclass(frozen=True)
class LineAmendment:
    """An issued line's new ordered quantity and prices (``...``: unchanged)."""

    line_id: UUID
    ordered_qty: Decimal | None = None
    unit_price: Decimal | EllipsisType | None = ...
    discount_pct: Decimal | EllipsisType | None = ...

    @property
    def prices(self) -> dict[str, Decimal | None]:
        return {
            name: value
            for name, value in (
                ("unit_price", self.unit_price),
                ("discount_pct", self.discount_pct),
            )
            if not isinstance(value, EllipsisType)
        }


@dataclass(frozen=True)
class AllocationAmendment:
    """An allocation's new ceiling: higher is an increase (C5), lower a trim."""

    allocation_id: UUID
    qty: Decimal


HEADER_AMENDMENTS = frozenset({"expected_date", "supplier_ref", "notes"})


def _payload(
    po_id: UUID,
    reason: str,
    header: dict[str, Any],
    lines: list[LineAmendment],
    allocations: list[AllocationAmendment],
    add: list[RequestItem],
) -> dict[str, Any]:
    return {
        "po_id": po_id,
        "reason": reason,
        "header": header,
        "lines": [
            {"line_id": item.line_id, "ordered_qty": item.ordered_qty, **item.prices}
            for item in lines
        ],
        "allocations": [{"id": item.allocation_id, "qty": item.qty} for item in allocations],
        "add": [{"request_line_id": item.request_line_id, "qty": item.qty} for item in add],
    }


def _amend_allocations(
    principal: Principal,
    order: PurchaseOrder,
    component: Component,
    amendments: list[AllocationAmendment],
    *,
    lower: bool,
) -> list[dict[str, Any]]:
    """Apply the decreases (``lower``) or the increases of ``amendments``."""
    done = []
    for item in amendments:
        allocation = PurchaseAllocation.objects.select_related("po_line__unit").get(
            pk=item.allocation_id
        )
        check_quantity(item.qty, allocation.po_line.unit, "qty", zero=True)
        before = allocation.qty
        if lower and item.qty < before:
            component.trim(principal, allocation, item.qty, TrimReason.UNALLOCATED, "amend")
        elif not lower and item.qty > before:
            _increase_allocation(principal, order, component, allocation, item.qty)
        else:
            continue
        done.append({"allocation_id": allocation.pk, "from": before, "to": item.qty})
    return done


def _amend_line(
    principal: Principal,
    order: PurchaseOrder,
    component: Component,
    item: LineAmendment,
) -> dict[str, Any] | None:
    """An issued line's quantity (down to what is received) and prices (no receipt yet)."""
    line = PurchaseOrderLine.objects.select_related("unit").get(pk=item.line_id)
    if line.closed_short_at is not None:
        raise ValidationError(_("This order line is closed short."), code="closed_short")
    prices = item.prices
    _check_prices(prices.get("unit_price"), prices.get("discount_pct"))
    diff = apply_changes(line, prices, {"unit_price", "discount_pct"})
    if diff and receipts.line_receipts([line.pk])[line.pk].posted:
        raise ValidationError(
            _("Line %(line)s has goods received: its price no longer changes."),
            code="price_after_receipt",
            params={"line": line.line_no},
        )
    if item.ordered_qty is not None and item.ordered_qty != line.ordered_qty:
        check_quantity(item.ordered_qty, line.unit, "ordered_qty")
        diff["ordered_qty"] = {"from": line.ordered_qty, "to": item.ordered_qty}
        if item.ordered_qty < line.ordered_qty:
            _reduce_ordered(principal, component, line, item.ordered_qty, "amend")
        line.ordered_qty = item.ordered_qty
    if not diff:
        return None
    _save_line(line)
    return {"line_id": line.pk, "line_no": line.line_no, "changes": diff}


@transactional
def amend(
    principal: Principal,
    po_id: UUID,
    *,
    reason: str,
    expected_date: date | EllipsisType | None = ...,
    supplier_ref: str | None = None,
    notes: str | None = None,
    lines: list[LineAmendment] | None = None,
    allocations: list[AllocationAmendment] | None = None,
    add: list[RequestItem] | None = None,
    idempotency_key: str | None = None,
) -> PurchaseOrder:
    """Amend an issued order (§4.4): ``revision + 1``, a reason, audited; a no-op without a
    change.

    Allowed: expected date, supplier reference, notes; ordered quantities (down to what is
    received; allocations above it are trimmed first); prices of lines with no posted
    receipt; allocations raised (C5) or trimmed; request lines added (with allocations,
    aggregated per material). Never the supplier, the buyer or the destination: cancel and
    create a new order instead. Snapshots refresh only for what changed (new lines).
    """
    reason = _reason(reason)
    lines, allocations, add = list(lines or ()), list(allocations or ()), list(add or ())
    header: dict[str, Any] = {
        name: value
        for name, value in (
            ("expected_date", expected_date),
            ("supplier_ref", supplier_ref.strip() if supplier_ref is not None else None),
            ("notes", notes.strip() if notes is not None else None),
        )
        if value is not ... and (value is not None or name == "expected_date")
    }
    for listed in ([item.line_id for item in lines], [item.allocation_id for item in allocations]):
        if len(set(listed)) != len(listed):
            raise ValueError("Each line and allocation is amended once per call")
    locks = LockOrder()
    order = _order(po_id)
    amended = PurchaseAllocation.objects.filter(
        pk__in=[item.allocation_id for item in allocations]
    ).values_list("request_line_id", flat=True)
    request_lines = {item.request_line_id for item in add} | set(amended)
    prices = any(item.prices for item in lines)
    _authorize(principal, order, locks, *_requests(request_lines), prices=prices)

    def execute() -> PurchaseOrder:
        locked, component = _lock_order(locks, order, request_lines=request_lines)
        if locked.status != Status.ISSUED:
            raise _status_error(locked)
        known_lines = set(locked.lines.values_list("pk", flat=True))
        if {item.line_id for item in lines} - known_lines or (
            PurchaseAllocation.objects.filter(pk__in=[a.allocation_id for a in allocations])
            .exclude(po_line__po=locked)
            .exists()
            or len(amended) != len(allocations)
        ):
            raise ValidationError(_("A line or allocation is not on this order."), code="unknown")
        changes: dict[str, Any] = {}
        if diff := apply_changes(locked, header, HEADER_AMENDMENTS):
            locked.full_clean(validate_constraints=False)
            changes["header"] = diff
        if trimmed := _amend_allocations(principal, locked, component, allocations, lower=True):
            changes["trimmed"] = trimmed
        if amended_lines := [
            change
            for item in lines
            if (change := _amend_line(principal, locked, component, item)) is not None
        ]:
            changes["lines"] = amended_lines
        if raised := _amend_allocations(principal, locked, component, allocations, lower=False):
            changes["raised"] = raised
        if added := _add_items(principal, locked, component, add):
            changes["added"] = added
        if not changes:
            return locked
        transition(locked, Status.ISSUED, "amend")
        locked.revision += 1
        locked.save()
        _record(
            principal,
            "purchase_order.amended",
            locked,
            revision=locked.revision,
            reason=reason,
            changes=changes,
        )
        _settle_order(principal, locked, component)
        component.reconcile(principal, "purchase_order.amend")
        return locked

    return execute_once(
        actor_id=principal.user_id,
        key=idempotency_key,
        operation="purchase_order.amend",
        payload=_payload(po_id, reason, header, lines, allocations, add),
        execute=execute,
        replay=lambda _type, pk: PurchaseOrder.objects.get(pk=pk),
    )


# --- cancel and close -----------------------------------------------------------------


@transactional
def cancel(principal: Principal, po_id: UUID, *, reason: str = "") -> PurchaseOrder:
    """Discard a draft, or cancel an issued order that has no posted receipt (a reason is
    then required). Every allocation is trimmed to zero; unsent dispatches are cancelled."""
    locks = LockOrder()
    order = _order(po_id)
    _authorize(principal, order, locks)
    issued = order.status == Status.ISSUED
    locked, component = _lock_order(
        locks, order, between_orders_and_lines=lock_dispatches(order.pk) if issued else None
    )
    if (locked.status == Status.ISSUED) != issued:
        raise LockSetChanged("The order was issued or closed between reading and locking it")
    operation = "cancel" if issued else "discard"
    left = transition(locked, Status.CANCELLED, operation)
    if issued:
        reason = _reason(reason)
        posted = receipts.line_receipts(list(locked.lines.values_list("pk", flat=True)))
        if any(found.posted for found in posted.values()):
            raise ValidationError(
                _("Goods have been received on this order; it can no longer be cancelled."),
                code="has_receipts",
            )
    trim = TrimReason.ORDER_CANCELLED if issued else TrimReason.DRAFT_DISCARDED
    order_lines = list(locked.lines.values_list("pk", flat=True))
    component.trim_to_attributed(
        principal, order_lines=order_lines, reason=trim, cause=f"purchase_order.{operation}"
    )
    dispatches = cancel_dispatches(locked) if issued else 0
    locked.save()
    action = "purchase_order.cancelled" if issued else "purchase_order.discarded"
    _record(
        principal,
        action,
        locked,
        from_status=left,
        reason=reason.strip(),
        dispatches_cancelled=dispatches,
        cancellation_notice_required=locked.cancellation_notice_required,
    )
    component.reconcile(principal, f"purchase_order.{operation}")
    return locked


@transactional
def close_line_short(principal: Principal, po_line_id: UUID, *, reason: str) -> PurchaseOrderLine:
    """Give up the rest of an issued order line (agreed with the supplier).

    Its allocations are trimmed to what is attributed (E1); the order closes once no line
    has capacity left (§4.9).
    """
    reason = _reason(reason)
    locks = LockOrder()
    order = _order(PurchaseOrderLine.objects.get(pk=po_line_id).po_id)
    _authorize(principal, order, locks)
    locked, component = _lock_order(locks, order)
    _require_issued(locked)
    line = PurchaseOrderLine.objects.get(pk=po_line_id)
    if line.closed_short_at is not None:
        raise ValidationError(_("This order line is already closed short."), code="closed_short")
    if component.book().order_lines[line.pk].remaining <= 0:
        raise ValidationError(_("This order line has been received in full."), code="received")
    line.closed_short_at, line.close_reason = db_now(), reason
    line.save()
    component.trim_to_attributed(
        principal,
        order_lines=[line.pk],
        reason=TrimReason.ORDER_LINE_CLOSED,
        cause="purchase_order_line.close_short",
    )
    _record(
        principal,
        "purchase_order_line.closed_short",
        locked,
        line_id=line.pk,
        line_no=line.line_no,
        reason=reason,
    )
    _settle_order(principal, locked, component)
    component.reconcile(principal, "purchase_order_line.close_short")
    return line
