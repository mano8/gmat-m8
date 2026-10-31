"""Shared rules of the purchasing services: authorization, locks, C5, C6, S9, snapshots."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db.models import Max, Model
from django.utils.translation import gettext_lazy as _

from apps.access.authorization import authorize_in_txn
from apps.access.principals import Principal
from apps.access.roles import Capability
from apps.accounts.models import User
from apps.audit.recorder import record
from apps.catalog.models import Material
from apps.catalog.quantities import check_quantity
from apps.core import events
from apps.core.db import constraint_errors, db_now
from apps.core.locking import LockLevel, LockOrder
from apps.core.sequences import format_number, next_number
from apps.locations.models import Location
from apps.material_requests.models import MaterialRequest, MaterialRequestLine
from apps.organizations.models import Branch
from apps.projects.models import Project
from apps.purchasing import receipts
from apps.purchasing.components import Component, TrimReason
from apps.purchasing.models import PurchaseAllocation, PurchaseOrder, PurchaseOrderLine
from apps.purchasing.transitions import transition
from apps.purchasing.waterfall import ZERO, Book
from apps.suppliers.models import Supplier

Status = PurchaseOrder.Status
VIEW, MANAGE, PRICE = Capability.PO_VIEW, Capability.PO_MANAGE, Capability.PRICE_VIEW

C9_FOREIGN_KEY = "po_line_material_unit_fk"
ONE_OPEN_LINE = "po_line_one_open_per_material"
LINE_CONSTRAINTS = {
    C9_FOREIGN_KEY: (
        "material",
        _("This material's unit has just changed; choose the material again."),
    ),
    ONE_OPEN_LINE: ("material", _("This order already has a line for this material.")),
}


@dataclass(frozen=True)
class RequestItem:
    """A request line to purchase on an order: ``qty`` (default: its *to purchase*)."""

    request_line_id: UUID
    qty: Decimal | None = None


# --- authorization and locks ----------------------------------------------------------


def _authorize(
    principal: Principal, obj: Model, locks: LockOrder, *others: Model, prices: bool = False
) -> User:
    """``po.manage`` on the order (or the branch of a new one) and ``others``.

    ``po.view`` is the read capability: an object outside it is 404, inside it without
    ``po.manage`` 403 (S7). Setting a price also needs ``price.view`` (plan §3.4).
    """
    actor = authorize_in_txn(principal, MANAGE, obj, *others, locks=locks, read=VIEW)
    if prices:
        authorize_in_txn(principal, PRICE, obj, locks=locks, read=VIEW)
    return actor


def _order(po_id: UUID) -> PurchaseOrder:
    return PurchaseOrder.objects.get(pk=po_id)


def _requests(request_line_ids: set[UUID]) -> list[MaterialRequest]:
    """The requests of these lines, to authorize (a missing line is 404)."""
    lines = MaterialRequestLine.objects.filter(pk__in=request_line_ids).select_related("request")
    found = {line.pk: line.request for line in lines}
    missing = request_line_ids - set(found)
    if missing:
        raise MaterialRequestLine.DoesNotExist(f"Request lines not found: {sorted(missing)}")
    return list({request.pk: request for request in found.values()}.values())


def _lock_order(
    locks: LockOrder, order: PurchaseOrder, **seeds: Any
) -> tuple[PurchaseOrder, Component]:
    """Lock the order's component (L2-L5, verified) and re-read the order under it."""
    component = Component.lock(locks, orders=[order.pk], **seeds)
    locked = PurchaseOrder.objects.select_related(
        "branch__company", "supplier", "ship_to__project", "ship_to__branch"
    ).get(pk=order.pk)
    return locked, component


# --- rules ----------------------------------------------------------------------------


def _status_error(order: PurchaseOrder) -> ValidationError:
    return ValidationError(
        _("This purchase order is %(status)s; it cannot be changed this way."),
        code="transition",
        params={"status": Status(order.status).label},
    )


def _require_draft(order: PurchaseOrder) -> None:
    """Drafts change freely; an issued order only through an amendment (§4.4)."""
    if order.status == Status.ISSUED:
        raise ValidationError(_("An issued order changes only through an amendment."), code="amend")
    if order.status != Status.DRAFT:
        raise _status_error(order)


def _require_issued(order: PurchaseOrder) -> None:
    if order.status != Status.ISSUED:
        raise _status_error(order)


def _reason(text: str, field: str = "reason") -> str:
    text = text.strip()
    if not text:
        raise ValidationError({field: ValidationError(_("Give a reason."), code="required")})
    return text


def _require_active_branch(branch: Branch) -> None:
    """S9: an inactive branch, or a branch of an inactive company, takes no new documents."""
    if not (branch.is_active and branch.company.is_active):
        raise ValidationError(_("This branch is inactive."), code="inactive_branch")


def _active_supplier(supplier_id: UUID) -> Supplier:
    supplier = Supplier.objects.filter(pk=supplier_id).first()
    if supplier is None or not supplier.is_active:
        raise ValidationError(
            {"supplier": ValidationError(_("Choose an active supplier."), code="inactive")}
        )
    return supplier


def _check_ship_to(branch: Branch, location: Location) -> None:
    """Where an order of ``branch`` may ship to (plan §4.6, C7 for its receipts).

    A warehouse of the branch or of a branch it manages, or the worksite of an active
    project that the branch buys for.
    """
    if location.kind == Location.Kind.WAREHOUSE:
        valid = location.branch_id == branch.pk or location.branch.managed_by_id == branch.pk
    else:
        project = location.project
        valid = (
            project is not None
            and project.procurement_branch_id == branch.pk
            and project.status == Project.Status.ACTIVE
        )
    # S9: an inactive branch takes no deliveries, at its warehouses or its worksites.
    if not (location.is_active and location.branch.is_active and valid):
        raise ValidationError(
            {"ship_to": ValidationError(_("Choose a valid destination."), code="ship_to")}
        )


def _check_worksite_projects(order: PurchaseOrder, location: Location) -> None:
    """C6: an order shipping to a worksite serves that project's requests only."""
    if location.kind != Location.Kind.WORKSITE:
        return
    others = PurchaseAllocation.objects.filter(po_line__po=order, qty__gt=0).exclude(
        request_line__project_id=cast(UUID, location.project_id)
    )
    if others.exists():
        raise ValidationError(
            {
                "ship_to": ValidationError(
                    _("This order serves other projects; it cannot ship to this worksite."),
                    code="worksite_projects",
                )
            }
        )


def _require_open_project(project: Project) -> None:
    """S9: only an active project of active branches and companies takes allocations."""
    if project.status != Project.Status.ACTIVE:
        raise ValidationError(
            _("The request's project accepts no new allocations."), code="project_not_active"
        )
    for branch in (project.branch, project.procurement_branch):
        _require_active_branch(branch)


def _request_line(line_id: UUID) -> MaterialRequestLine:
    return MaterialRequestLine.objects.select_related(
        "request",
        "unit",
        "material",
        "project__branch__company",
        "project__procurement_branch__company",
    ).get(pk=line_id)


def _to_purchase(book: Book, delivered: dict[UUID, Decimal], line: MaterialRequestLine) -> Decimal:
    """max(0, A - committed) of an open ``PURCHASE`` line (plan §4.5): callers check
    ``_check_purchasable`` first."""
    facts = book.request_lines[line.pk]
    return max(ZERO, facts.approved - book.committed(line.pk, delivered.get(line.pk, ZERO)))


def _check_purchasable(order: PurchaseOrder, line: MaterialRequestLine, book: Book) -> None:
    """C5 (request side), C6 and S9: ``line`` may be allocated on ``order`` at all."""
    facts = book.request_lines.get(line.pk)
    if facts is None or not facts.is_open:
        raise ValidationError(
            _("Line %(line)s of %(request)s is not open for purchasing."),
            code="not_open",
            params={"line": line.line_no, "request": line.request},
        )
    if line.sourcing != MaterialRequestLine.Sourcing.PURCHASE:
        raise ValidationError(
            _("Line %(line)s of %(request)s is not to be purchased."),
            code="not_purchase",
            params={"line": line.line_no, "request": line.request},
        )
    if line.request.procurement_branch_id != order.branch_id:
        raise ValidationError(
            _("%(request)s is bought by another branch."),
            code="branch_mismatch",
            params={"request": line.request},
        )
    _require_open_project(line.project)
    if order.ship_to.kind == Location.Kind.WORKSITE and order.ship_to.project_id != line.project_id:
        raise ValidationError(
            _("This order ships to another project's worksite."), code="worksite_projects"
        )


def _check_fits(
    book: Book, delivered: dict[UUID, Decimal], line: MaterialRequestLine, increase: Decimal
) -> None:
    """C5: the increase fits the line's *to purchase*."""
    available = _to_purchase(book, delivered, line)
    if increase > available:
        raise ValidationError(
            {
                "qty": ValidationError(
                    _("Only %(available)s are left to purchase for this line."),
                    code="exceeds_to_purchase",
                    params={"available": f"{available.normalize():f}"},
                )
            }
        )


def _check_request_side(
    order: PurchaseOrder,
    line: MaterialRequestLine,
    book: Book,
    delivered: dict[UUID, Decimal],
    increase: Decimal,
) -> None:
    """C5 (request side), C6 and S9 for allocating ``increase`` more to ``line``."""
    _check_purchasable(order, line, book)
    _check_fits(book, delivered, line, increase)


def _check_order_side(
    order: PurchaseOrder, po_line: PurchaseOrderLine, book: Book, increase: Decimal
) -> None:
    """C5 (order side) and I3: an open line with room for ``increase`` more."""
    if po_line.closed_short_at is not None:
        raise ValidationError(_("This order line is closed short."), code="closed_short")
    totals = book.order_line_totals(po_line.pk)
    if order.status == Status.ISSUED and not totals.has_capacity:
        raise ValidationError(_("This order line has nothing left to receive."), code="no_capacity")
    if totals.allocated + increase > po_line.ordered_qty:
        raise ValidationError(
            {
                "qty": ValidationError(
                    _("The allocations would exceed the ordered quantity."),
                    code="exceeds_ordered",
                )
            }
        )


def _same_material(po_line: PurchaseOrderLine, line: MaterialRequestLine) -> None:
    if po_line.material_id != line.material_id:
        raise ValidationError(
            _("The order line and the request line are different materials."),
            code="material_mismatch",
        )


def _save_line(line: PurchaseOrderLine) -> None:
    line.full_clean(validate_constraints=False)
    with constraint_errors(LINE_CONSTRAINTS):
        line.save()


def _next_line_no(order: PurchaseOrder) -> int:
    return int(order.lines.aggregate(last=Max("line_no"))["last"] or 0) + 1


def _require_active_material(material: Material) -> None:
    if not material.is_active:
        raise ValidationError(
            {"material": ValidationError(_("This material is inactive."), code="inactive")}
        )


# --- snapshots, numbers, audit --------------------------------------------------------


def _material_snapshot(material: Material) -> dict[str, Any]:
    return {"reference": material.reference, "name": material.name}


def _order_snapshots(order: PurchaseOrder) -> dict[str, dict[str, Any]]:
    """What issued documents show of the buyer, the supplier and the destination."""
    branch, company = order.branch, order.branch.company
    supplier, location = order.supplier, order.ship_to
    project = location.project
    return {
        "buyer_snapshot": {
            "company": {
                "code": company.code,
                "legal_name": company.legal_name,
                "tax_id": company.tax_id,
            },
            "branch": {
                "code": branch.code,
                "name": branch.name,
                "city": branch.city,
                "address": branch.address,
            },
        },
        "supplier_snapshot": {
            "name": supplier.name,
            "tax_id": supplier.tax_id,
            "contact_name": supplier.contact_name,
            "email": supplier.email,
            "phone": supplier.phone,
            "address": supplier.address,
        },
        "ship_to_snapshot": {
            "kind": location.kind,
            "name": location.name,
            "address": location.address,
            "project": (
                {"code": project.code, "name": project.name} if project is not None else None
            ),
        },
    }


def _assign_number(order: PurchaseOrder, locks: LockOrder) -> None:
    """``PED-{company}-{yyyy}-{00001}`` per buyer company (L9, last)."""
    code, year = order.branch.company.code, db_now().year
    locks.enter(LockLevel.DOCUMENT_SEQUENCE)
    value = next_number(f"po:{code}:{year}")
    order.number = format_number(settings.PURCHASE_ORDER_NUMBER_PREFIX, code, year, value)


def _record(principal: Principal, action: str, order: PurchaseOrder, **data: Any) -> None:
    record(
        principal.audit_identity,
        action,
        obj=order,
        branch_id=order.branch_id,
        data={"number": order.number, **data},
    )
    events.publish(action, po_id=order.pk)


def _quantity(value: Decimal) -> str:
    return f"{value.normalize():f}"


# --- lines and allocations ------------------------------------------------------------


def _add_items(
    principal: Principal,
    order: PurchaseOrder,
    component: Component,
    items: list[RequestItem],
) -> list[dict[str, Any]]:
    """Purchase request lines on ``order``, aggregated per material (one open line each).

    Each item raises (or creates) the order's open line of its material and the
    allocation of its request line by the same quantity, within C5. Returns what changed.
    """
    added: list[dict[str, Any]] = []
    for item in items:
        line = _request_line(item.request_line_id)
        book = component.book()
        delivered = component.delivered(book)
        _check_purchasable(order, line, book)
        quantity = item.qty
        if quantity is None:
            quantity = _to_purchase(book, delivered, line)
            if quantity <= 0:
                raise ValidationError(
                    _("Nothing is left to purchase for line %(line)s of %(request)s."),
                    code="nothing_to_purchase",
                    params={"line": line.line_no, "request": line.request},
                )
        check_quantity(quantity, line.unit, "qty")
        _check_fits(book, delivered, line, quantity)
        material = cast(Material, line.material)  # a PURCHASE line has one (CHECK)
        po_line = order.lines.filter(material=material, closed_short_at=None).first()
        if po_line is None:
            _require_active_material(material)
            po_line = PurchaseOrderLine(
                po=order,
                line_no=_next_line_no(order),
                material=material,
                unit_id=line.unit_id,
                ordered_qty=quantity,
                material_snapshot=(
                    _material_snapshot(material) if order.status != Status.DRAFT else {}
                ),
            )
            _save_line(po_line)
            component.add_order_line(po_line.pk)
            action = "purchase_order_line.added"
        else:
            po_line.ordered_qty += quantity
            _save_line(po_line)
            action = "purchase_order_line.increased"
        _record(
            principal,
            action,
            order,
            line_id=po_line.pk,
            line_no=po_line.line_no,
            material_id=po_line.material_id,
            ordered_qty=po_line.ordered_qty,
        )
        allocation, created = PurchaseAllocation.objects.get_or_create(
            po_line=po_line, request_line=line, defaults={"qty": quantity}
        )
        if not created:
            allocation.qty += quantity
            allocation.save(update_fields=["qty", "updated_at"])
        _record_allocation(principal, order, allocation, quantity, created=created)
        added.append(
            {
                "request_line_id": line.pk,
                "line_no": po_line.line_no,
                "qty": quantity,
            }
        )
    return added


def _record_allocation(
    principal: Principal,
    order: PurchaseOrder,
    allocation: PurchaseAllocation,
    increase: Decimal,
    *,
    created: bool,
) -> None:
    action = "purchase_allocation.created" if created else "purchase_allocation.increased"
    record(
        principal.audit_identity,
        action,
        obj=allocation,
        branch_id=order.branch_id,
        data={
            "order_id": order.pk,
            "order": order.number,
            "request_line_id": allocation.request_line_id,
            "increase": increase,
            "qty": allocation.qty,
        },
    )
    events.publish(
        action,
        allocation_id=allocation.pk,
        po_id=order.pk,
        request_line_id=allocation.request_line_id,
    )


def _increase_allocation(
    principal: Principal,
    order: PurchaseOrder,
    component: Component,
    allocation: PurchaseAllocation,
    qty: Decimal,
) -> Decimal:
    """Raise ``allocation`` to ``qty`` within C5; return the increase."""
    po_line = PurchaseOrderLine.objects.get(pk=allocation.po_line_id)
    line = _request_line(allocation.request_line_id)
    check_quantity(qty, line.unit, "qty")
    increase: Decimal = qty - allocation.qty
    book = component.book()
    _check_order_side(order, po_line, book, increase)
    _check_request_side(order, line, book, component.delivered(book), increase)
    allocation.qty = qty
    allocation.save(update_fields=["qty", "updated_at"])
    _record_allocation(principal, order, allocation, increase, created=False)
    return increase


def _reduce_ordered(
    principal: Principal,
    component: Component,
    po_line: PurchaseOrderLine,
    ordered: Decimal,
    cause: str,
) -> None:
    """Lower an ordered quantity, never below what is received; allocations above it are
    trimmed first, in reverse attribution order (§4.4, §4.5)."""
    received = receipts.line_receipts([po_line.pk])[po_line.pk].received
    if ordered < received:
        raise ValidationError(
            {
                "ordered_qty": ValidationError(
                    _("%(received)s have already been received on this line."),
                    code="below_received",
                    params={"received": _quantity(received)},
                )
            }
        )
    po_line.ordered_qty = ordered
    _save_line(po_line)
    book = component.book()
    listed = book.of_order_line(po_line.pk)
    total = sum((allocation.qty for allocation in listed), ZERO)
    for facts in reversed(listed):
        if total <= ordered:
            break
        target = max(facts.attributed, facts.qty - (total - ordered))
        allocation = PurchaseAllocation.objects.get(pk=facts.id)
        component.trim(principal, allocation, target, TrimReason.ORDERED_QTY_REDUCED, cause)
        total -= facts.qty - target


def _check_prices(unit_price: Any, discount_pct: Any) -> None:
    if unit_price is not None and (not isinstance(unit_price, Decimal) or unit_price < 0):
        raise ValidationError(
            {"unit_price": ValidationError(_("Enter a price of zero or more."), code="invalid")}
        )
    if discount_pct is not None and (
        not isinstance(discount_pct, Decimal) or not 0 <= discount_pct <= 100
    ):
        raise ValidationError(
            {
                "discount_pct": ValidationError(
                    _("Enter a discount between 0 and 100 %."), code="invalid"
                )
            }
        )


def _settle_order(principal: Principal, order: PurchaseOrder, component: Component) -> None:
    """Automatic close and reopen (§4.9) after a change to line capacity.

    ``ISSUED → CLOSED`` when no line has capacity; ``CLOSED → ISSUED`` when one has again
    (a receipt void, M6.1). Both are recorded as the actor's.
    """
    book = component.book()
    capacity = any(
        line.has_capacity for line in book.order_lines.values() if line.po_id == order.pk
    )
    if order.status == Status.ISSUED and not capacity:
        transition(order, Status.CLOSED, "auto_close")
        action = "purchase_order.auto_closed"
    elif order.status == Status.CLOSED and capacity:
        transition(order, Status.ISSUED, "auto_reopen")
        action = "purchase_order.auto_reopened"
    else:
        return
    order.save()
    _record(principal, action, order)
