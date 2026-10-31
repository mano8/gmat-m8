"""Building a draft order: header, lines from request lines, allocations (plan §4.4, §4.5).

A draft changes freely (``DRAFT`` row of §4.4); each allocation reserves demand (Q), so two
drafts can never allocate the same approved quantity (E9). An issued order changes only
through ``orders.amend``.
"""

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
from apps.core.idempotency import execute_once
from apps.core.locking import LockLevel, LockOrder, transactional
from apps.locations.models import Location
from apps.organizations.models import Branch
from apps.purchasing.components import Component, TrimReason
from apps.purchasing.models import PurchaseAllocation, PurchaseOrder, PurchaseOrderLine
from apps.purchasing.services.common import (
    RequestItem,
    _active_supplier,
    _add_items,
    _authorize,
    _check_order_side,
    _check_prices,
    _check_request_side,
    _check_ship_to,
    _check_worksite_projects,
    _increase_allocation,
    _lock_order,
    _order,
    _quantity,
    _record,
    _record_allocation,
    _reduce_ordered,
    _request_line,
    _requests,
    _require_active_branch,
    _require_draft,
    _same_material,
    _save_line,
)

HEADER_FIELDS = frozenset({"supplier_id", "ship_to_id", "expected_date", "supplier_ref", "notes"})


@transactional
def create_po(
    principal: Principal,
    *,
    branch_id: UUID,
    supplier_id: UUID,
    ship_to_id: UUID,
    expected_date: date | None = None,
    supplier_ref: str = "",
    notes: str = "",
    idempotency_key: str | None = None,
) -> PurchaseOrder:
    """Start a draft order of ``branch`` (its company is the buyer, Q2) to a supplier.

    It ships to a warehouse of the branch (or of a branch it manages) or to the worksite of
    an active project the branch buys for.
    """
    locks = LockOrder()
    branch = Branch.objects.get(pk=branch_id)
    ship_to = Location.objects.get(pk=ship_to_id)
    _authorize(principal, branch, locks, ship_to)
    payload = {
        "branch_id": branch_id,
        "supplier_id": supplier_id,
        "ship_to_id": ship_to_id,
        "expected_date": expected_date,
        "supplier_ref": supplier_ref,
        "notes": notes,
    }

    def execute() -> PurchaseOrder:
        # Under the L0 lock: the branch's state and scope cannot change now (S9).
        current = Branch.objects.select_related("company").get(pk=branch_id)
        _require_active_branch(current)
        supplier = _active_supplier(supplier_id)
        location = Location.objects.select_related("branch", "project").get(pk=ship_to_id)
        _check_ship_to(current, location)
        order = PurchaseOrder(
            branch=current,
            buyer_company_id=current.company_id,
            supplier=supplier,
            ship_to=location,
            expected_date=expected_date,
            supplier_ref=supplier_ref.strip(),
            notes=notes.strip(),
        )
        order.full_clean(validate_constraints=False)
        order.save()
        _record(
            principal,
            "purchase_order.created",
            order,
            supplier_id=supplier.pk,
            ship_to_id=location.pk,
        )
        return order

    return execute_once(
        actor_id=principal.user_id,
        key=idempotency_key,
        operation="purchase_order.create",
        payload=payload,
        execute=execute,
        replay=lambda _type, pk: PurchaseOrder.objects.get(pk=pk),
    )


@transactional
def update_order(principal: Principal, po_id: UUID, **changes: Any) -> PurchaseOrder:
    """Change a draft's supplier, destination, expected date, supplier reference or notes.

    A worksite destination needs every allocation to be for that project (C6).
    """
    unknown = sorted(set(changes) - HEADER_FIELDS)
    if unknown:
        raise ValueError(f"Purchase order fields that cannot be changed here: {unknown}")
    locks = LockOrder()
    order = _order(po_id)
    others = [Location.objects.get(pk=changes["ship_to_id"])] if "ship_to_id" in changes else []
    _authorize(principal, order, locks, *others)
    locks.lock(LockLevel.PO_HEADERS, PurchaseOrder, [order.pk])
    locked = PurchaseOrder.objects.select_related("branch__company").get(pk=order.pk)
    _require_draft(locked)
    for name in ("supplier_ref", "notes"):
        if isinstance(changes.get(name), str):
            changes[name] = changes[name].strip()
    diff = apply_changes(locked, changes, HEADER_FIELDS)
    if not diff:
        return locked
    if "supplier_id" in diff:
        _active_supplier(locked.supplier_id)
    if "ship_to_id" in diff:
        location = Location.objects.select_related("branch", "project").get(pk=locked.ship_to_id)
        _check_ship_to(locked.branch, location)
        _check_worksite_projects(locked, location)
    locked.full_clean(validate_constraints=False)
    locked.save()
    _record(principal, "purchase_order.updated", locked, changes=diff)
    return locked


@transactional
def add_request_lines(
    principal: Principal,
    po_id: UUID,
    items: list[RequestItem],
    *,
    idempotency_key: str | None = None,
) -> PurchaseOrder:
    """Purchase request lines on a draft, aggregated per material (20 + 15 + 10 = 45).

    Each item allocates ``qty`` (default: the line's *to purchase*) and raises the order's
    line of that material by as much (C5: open ``PURCHASE`` line, same material and
    procurement branch, within *to purchase*).
    """
    ids = [item.request_line_id for item in items]
    if len(set(ids)) != len(ids):
        raise ValueError("Each request line is added once per call")
    if not ids:
        raise ValidationError(_("Choose at least one request line."), code="empty")
    locks = LockOrder()
    order = _order(po_id)
    _authorize(principal, order, locks, *_requests(set(ids)))
    payload = {
        "po_id": po_id,
        "items": [{"request_line_id": item.request_line_id, "qty": item.qty} for item in items],
    }

    def execute() -> PurchaseOrder:
        locked, component = _lock_order(locks, order, request_lines=ids)
        _require_draft(locked)
        _add_items(principal, locked, component, items)
        component.reconcile(principal, "purchase_order.add_request_lines")
        return locked

    return execute_once(
        actor_id=principal.user_id,
        key=idempotency_key,
        operation="purchase_order.add_request_lines",
        payload=payload,
        execute=execute,
        replay=lambda _type, pk: PurchaseOrder.objects.get(pk=pk),
    )


@transactional
def allocate(
    principal: Principal,
    po_line_id: UUID,
    *,
    request_line_id: UUID,
    qty: Decimal,
    idempotency_key: str | None = None,
) -> PurchaseAllocation:
    """Allocate part of a draft line to a request line (C5; I3: within the ordered quantity)."""
    locks = LockOrder()
    po_line = PurchaseOrderLine.objects.get(pk=po_line_id)
    order = _order(po_line.po_id)
    _authorize(principal, order, locks, *_requests({request_line_id}))
    payload = {"po_line_id": po_line_id, "request_line_id": request_line_id, "qty": qty}

    def execute() -> PurchaseAllocation:
        locked, component = _lock_order(locks, order, request_lines=[request_line_id])
        _require_draft(locked)
        current = PurchaseOrderLine.objects.get(pk=po_line_id)
        line = _request_line(request_line_id)
        if PurchaseAllocation.objects.filter(po_line=current, request_line=line).exists():
            raise ValidationError(
                _("This request line is already allocated here; change that allocation."),
                code="allocated",
            )
        check_quantity(qty, line.unit, "qty")
        _same_material(current, line)
        book = component.book()
        _check_order_side(locked, current, book, qty)
        _check_request_side(locked, line, book, component.delivered(book), qty)
        allocation = PurchaseAllocation.objects.create(po_line=current, request_line=line, qty=qty)
        _record_allocation(principal, locked, allocation, qty, created=True)
        component.reconcile(principal, "purchase_allocation.allocate")
        return allocation

    return execute_once(
        actor_id=principal.user_id,
        key=idempotency_key,
        operation="purchase_allocation.allocate",
        payload=payload,
        execute=execute,
        replay=lambda _type, pk: PurchaseAllocation.objects.get(pk=pk),
    )


def _locked_allocation(
    principal: Principal, allocation_id: UUID, locks: LockOrder
) -> tuple[PurchaseOrder, Component, PurchaseAllocation]:
    """Authorize on the allocation's order and request, lock the component, re-read it."""
    allocation = PurchaseAllocation.objects.select_related("po_line").get(pk=allocation_id)
    order = _order(allocation.po_line.po_id)
    _authorize(principal, order, locks, *_requests({allocation.request_line_id}))
    locked, component = _lock_order(locks, order, request_lines=[allocation.request_line_id])
    _require_draft(locked)
    current = PurchaseAllocation.objects.select_related("po_line__unit").get(pk=allocation_id)
    return locked, component, current


@transactional
def increase_allocation(
    principal: Principal, allocation_id: UUID, *, qty: Decimal
) -> PurchaseAllocation:
    """Raise a draft allocation to ``qty`` (C5); the same quantity again is a no-op."""
    locks = LockOrder()
    locked, component, allocation = _locked_allocation(principal, allocation_id, locks)
    if qty == allocation.qty:
        return allocation
    if qty < allocation.qty:
        raise ValidationError(
            {"qty": ValidationError(_("Use unallocate to lower an allocation."), code="not_higher")}
        )
    _increase_allocation(principal, locked, component, allocation, qty)
    component.reconcile(principal, "purchase_allocation.increase")
    return allocation


@transactional
def unallocate(
    principal: Principal, allocation_id: UUID, *, qty: Decimal = Decimal("0")
) -> PurchaseAllocation:
    """Lower a draft allocation to ``qty`` (zero by default): an audited trim."""
    locks = LockOrder()
    _locked, component, allocation = _locked_allocation(principal, allocation_id, locks)
    check_quantity(qty, allocation.po_line.unit, "qty", zero=True)
    if qty == allocation.qty:
        return allocation
    if qty > allocation.qty:
        raise ValidationError(
            {"qty": ValidationError(_("An allocation can only be lowered here."), code="not_lower")}
        )
    cause = "purchase_allocation.unallocate"
    component.trim(principal, allocation, qty, TrimReason.UNALLOCATED, cause)
    component.reconcile(principal, "purchase_allocation.unallocate")
    return allocation


@transactional
def update_line(
    principal: Principal,
    po_line_id: UUID,
    *,
    ordered_qty: Decimal | None = None,
    unit_price: Decimal | EllipsisType | None = ...,
    discount_pct: Decimal | EllipsisType | None = ...,
) -> PurchaseOrderLine:
    """Change a draft line's ordered quantity or price (``...``: unchanged; ``None`` clears a
    price). Lowering the quantity below its allocations trims them, newest in the
    attribution order first."""
    prices = {
        name: value
        for name, value in (("unit_price", unit_price), ("discount_pct", discount_pct))
        if value is not ...
    }
    locks = LockOrder()
    po_line = PurchaseOrderLine.objects.get(pk=po_line_id)
    order = _order(po_line.po_id)
    _authorize(principal, order, locks, prices=bool(prices))
    locked, component = _lock_order(locks, order)
    _require_draft(locked)
    current = PurchaseOrderLine.objects.select_related("unit").get(pk=po_line_id)
    _check_prices(prices.get("unit_price"), prices.get("discount_pct"))
    diff = apply_changes(current, prices, {"unit_price", "discount_pct"})
    if ordered_qty is not None and ordered_qty != current.ordered_qty:
        check_quantity(ordered_qty, current.unit, "ordered_qty")
        diff["ordered_qty"] = {"from": current.ordered_qty, "to": ordered_qty}
        if ordered_qty < current.ordered_qty:
            _reduce_ordered(
                principal, component, current, ordered_qty, "purchase_order_line.update"
            )
        current.ordered_qty = ordered_qty
    if not diff:
        return current
    _save_line(current)
    _record(
        principal,
        "purchase_order_line.updated",
        locked,
        line_id=current.pk,
        line_no=current.line_no,
        changes=diff,
    )
    component.reconcile(principal, "purchase_order_line.update")
    return current


@transactional
def remove_line(principal: Principal, po_line_id: UUID) -> PurchaseOrder:
    """Delete a draft line and its reservations (nothing refers to a draft's lines yet)."""
    locks = LockOrder()
    po_line = PurchaseOrderLine.objects.get(pk=po_line_id)
    order = _order(po_line.po_id)
    _authorize(principal, order, locks)
    locked, component = _lock_order(locks, order)
    _require_draft(locked)
    current = PurchaseOrderLine.objects.get(pk=po_line_id)
    allocations = list(current.allocations.values_list("request_line_id", "qty"))
    current.allocations.all().delete()
    current.delete()
    _record(
        principal,
        "purchase_order_line.removed",
        locked,
        line_id=po_line_id,
        line_no=current.line_no,
        material_id=current.material_id,
        ordered_qty=_quantity(current.ordered_qty),
        allocations=[
            {"request_line_id": line_id, "qty": _quantity(qty)} for line_id, qty in allocations
        ],
    )
    component.reconcile(principal, "purchase_order_line.remove")
    return locked
