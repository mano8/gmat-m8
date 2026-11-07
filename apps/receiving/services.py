"""Posting and voiding goods receipts (plan §4.5, §4.6, §4.9; ADR-0006).

Each service runs in one transaction with ``lock_timeout`` through the shared retry
wrapper, in the order of CLAUDE.md "Writes":

1. ``authorize_in_txn`` in full: ``receipt.post`` on what the receipt will belong to (below),
   ``receipt.void`` on the receipt; ``receipt.view`` is the read capability (404 outside
   it, 403 without the right);
2. the idempotency record of a posting (L1);
3. the order's allocation component (``purchasing.components``): every request header (L2)
   and line (L3), then every order header (L4) and line (L5) connected to the order,
   discovered, locked and verified; a void then locks the receipt (L6) and its direct
   handover (L7);
4. validation under those locks: the order is issued (C10), the location (C7, S9), each
   line (I4: never more than remains to be received), the attribution (§4.5, caps);
5. the numbers last (L9: the receipt's, then its direct handover's), then the rows;
6. the automatic close or reopen of the requests whose deliveries changed and of the order
   (§4.9), then the reconciliation of the component (§4.5);
7. audit and events: ``receipt.posted``, ``receipt.voided``, ``handover.posted``,
   ``handover.voided`` (§4.10).

**Who posts** (plan §3.4, Q9): a receipt is a document of its order's branch (S5); one at a
worksite also belongs to that project, which its site manager reaches (○ site). At a
warehouse the authorization object is the order. At a worksite it is the requests of that
project that the order serves: their procurement branch is the order's (C5) and their
project the worksite's, so each membership covers them exactly when it covers the receipt
(S6). A site manager therefore receives only at their project's worksite, from any order
that buys for it, and the goods go to that project's allocations only.

**Attribution** (``attribution``): at a warehouse in attribution order, at a worksite to the
project's allocations, each capped by its ceiling remaining and its request line's A - R
(and A - D on site); a manual split is checked against the same caps; the rest is surplus.
Each site attribution is handed over at once (``handovers.direct``, I8).

**Void**: the attributions stop counting, the direct handover is voided with the receipt,
the order reopens and the requests reopen when eligible, and the revived pending is
reconciled (E2, E4, E8).
"""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import UUID

from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.access.authorization import authorize_in_txn
from apps.access.principals import Principal
from apps.access.roles import Capability
from apps.accounts.models import User
from apps.audit.recorder import record
from apps.catalog.quantities import check_quantity
from apps.core import events
from apps.core.db import db_now
from apps.core.idempotency import execute_once
from apps.core.locking import LockLevel, LockOrder, transactional
from apps.core.sequences import format_number, next_number
from apps.handovers import direct
from apps.handovers.common import owner_code
from apps.locations.models import Location
from apps.material_requests.models import MaterialRequest, MaterialRequestLine
from apps.material_requests.services.common import _settle_closure
from apps.projects.models import Project
from apps.purchasing.components import Component
from apps.purchasing.models import PurchaseAllocation, PurchaseOrder, PurchaseOrderLine
from apps.purchasing.services.common import _settle_order
from apps.purchasing.waterfall import Book
from apps.receiving.attribution import Candidate, Capacity, default_split, manual_split
from apps.receiving.models import GoodsReceipt, GoodsReceiptLine, ReceiptAttribution
from apps.receiving.transitions import transition

VIEW, POST, VOID = Capability.RECEIPT_VIEW, Capability.RECEIPT_POST, Capability.RECEIPT_VOID
Status = GoodsReceipt.Status
Issue = GoodsReceiptLine.Issue
ZERO = Decimal("0")


@dataclass(frozen=True)
class ReceiptLineInput:
    """What arrived of one order line.

    ``qty_received`` is accepted (it counts as received), ``qty_rejected`` refused with an
    incident. ``split``: a manual attribution per allocation (Q14); ``None`` takes the
    default split.
    """

    po_line_id: UUID
    qty_received: Decimal
    qty_rejected: Decimal = ZERO
    issue: str = Issue.NONE
    issue_note: str = ""
    split: Mapping[UUID, Decimal] | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "po_line_id": self.po_line_id,
            "qty_received": self.qty_received,
            "qty_rejected": self.qty_rejected,
            "issue": self.issue,
            "issue_note": self.issue_note.strip(),
            "split": None
            if self.split is None
            else {str(pk): part for pk, part in self.split.items()},
        }


@dataclass
class _LinePlan:
    """A validated receipt line and its attribution."""

    po_line: PurchaseOrderLine
    item: ReceiptLineInput
    split: dict[UUID, Decimal]

    @property
    def surplus(self) -> Decimal:
        return self.item.qty_received - sum(self.split.values(), ZERO)


@dataclass
class _Facts:
    """What the attribution of one receipt reads, under the component's locks."""

    book: Book
    capacity: Capacity
    projects: dict[UUID, UUID]  # request line → project
    allocations: dict[UUID, PurchaseAllocation] = field(default_factory=dict)


# --- authorization --------------------------------------------------------------------


def _authorize_post(
    principal: Principal, order: PurchaseOrder, location: Location, locks: LockOrder
) -> User:
    """``receipt.post`` on what the receipt will belong to (see the module docstring)."""
    objects: list[Any] = [order]
    if location.project_id is not None:
        served = MaterialRequest.objects.filter(
            project_id=location.project_id,
            lines__allocations__po_line__po=order,
            lines__allocations__qty__gt=0,
        ).distinct()
        objects = list(served.order_by("pk")) or objects
    return authorize_in_txn(principal, POST, *objects, locks=locks, read=VIEW)


# --- rules ----------------------------------------------------------------------------


def _reason(text: str) -> str:
    text = text.strip()
    if not text:
        raise ValidationError({"reason": ValidationError(_("Give a reason."), code="required")})
    return text


def _location(location_id: UUID) -> Location:
    return Location.objects.select_related("branch__company", "project").get(pk=location_id)


def _check_order(order: PurchaseOrder) -> None:
    """C10, S9: goods arrive only for an issued order of an active branch."""
    if order.status != PurchaseOrder.Status.ISSUED:
        raise ValidationError(
            _("This purchase order is %(status)s; nothing can be received on it."),
            code="not_receivable",
            params={"status": PurchaseOrder.Status(order.status).label},
        )
    branch = order.branch
    if not (branch.is_active and branch.company.is_active):
        raise ValidationError(_("The order's branch is inactive."), code="inactive_branch")


def _invalid_location() -> ValidationError:
    return ValidationError(
        {"location": ValidationError(_("Goods of this order cannot arrive here."), code="location")}
    )


def _check_location(order: PurchaseOrder, location: Location) -> None:
    """C7 and S9: where goods of ``order`` may arrive.

    A warehouse of the order's branch scope (the branch or a branch it manages), or the
    worksite of a project with allocations on the order that is not ``CLOSED`` (the goods
    of a closed project arrive at a warehouse). Active locations of active branches only.
    """
    branch = location.branch
    if not (location.is_active and branch.is_active and branch.company.is_active):
        raise _invalid_location()
    if location.kind == Location.Kind.WAREHOUSE:
        if order.branch_id not in {branch.pk, branch.managed_by_id}:
            raise _invalid_location()
        return
    project = location.project
    served = PurchaseAllocation.objects.filter(
        po_line__po=order, qty__gt=0, request_line__project=project
    )
    if project is None or project.status == Project.Status.CLOSED or not served.exists():
        raise _invalid_location()


def _check_line_input(item: ReceiptLineInput, po_line: PurchaseOrderLine) -> None:
    check_quantity(item.qty_received, po_line.unit, "qty_received", zero=True)
    check_quantity(item.qty_rejected, po_line.unit, "qty_rejected", zero=True)
    if item.qty_received == 0 and item.qty_rejected == 0:
        raise ValidationError(
            _("Line %(line)s: enter what was accepted or rejected."),
            code="empty_line",
            params={"line": po_line.line_no},
        )
    if item.issue not in Issue.values:
        raise ValidationError({"issue": ValidationError(_("Choose an incident."), code="invalid")})
    for part in (item.split or {}).values():
        check_quantity(part, po_line.unit, "split", zero=True)


def _check_capacity(item: ReceiptLineInput, po_line: PurchaseOrderLine, book: Book) -> None:
    """C10 and I4: the line can still receive, and never more than remains.

    The order line's outstanding quantity is never put in the message: it covers every
    project the line serves, and a site manager may learn only what their own project can
    take (§3.4). Who holds ``po.view`` reads it on the form.
    """
    facts = book.order_lines[po_line.pk]
    if not facts.has_capacity:
        raise ValidationError(
            _("Line %(line)s has nothing left to receive."),
            code="no_capacity",
            params={"line": po_line.line_no},
        )
    if item.qty_received > facts.remaining:
        raise ValidationError(
            _("Line %(line)s: more than remains to be received on this order."),
            code="over_receipt",
            params={"line": po_line.line_no},
        )


def _candidates(facts: _Facts, po_line_id: UUID, project_id: UUID | None) -> list[Candidate]:
    """The allocations that may take goods of this line, in attribution order (§4.5)."""
    book = facts.book
    return [
        Candidate(allocation.id, allocation.request_line_id, allocation.ceiling_remaining)
        for allocation in book.of_order_line(po_line_id)
        if book.request_lines[allocation.request_line_id].is_open
        and allocation.ceiling_remaining > 0
        and (project_id is None or facts.projects[allocation.request_line_id] == project_id)
    ]


def _plan_line(
    order: PurchaseOrder, location: Location, facts: _Facts, item: ReceiptLineInput
) -> _LinePlan:
    po_line = (
        PurchaseOrderLine.objects.select_related("unit")
        .filter(pk=item.po_line_id, po=order)
        .first()
    )
    if po_line is None:
        raise ValidationError(_("A line is not on this order."), code="unknown")
    _check_line_input(item, po_line)
    candidates = _candidates(facts, po_line.pk, location.project_id)
    if item.split is None:
        split = default_split(item.qty_received, candidates, facts.capacity)
    else:
        split = manual_split(item.qty_received, item.split, candidates, facts.capacity)
    return _LinePlan(po_line, item, split)


def _facts(component: Component, location: Location) -> _Facts:
    book = component.book()
    delivered = component.delivered(book)
    lines = list(book.request_lines)
    received = {line: book.request_line_totals(line).received for line in lines}
    projects = dict(
        MaterialRequestLine.objects.filter(pk__in=lines).values_list("pk", "project_id")
    )
    return _Facts(
        book=book,
        capacity=Capacity(
            {line: facts.approved for line, facts in book.request_lines.items()},
            received,
            delivered,
            site=location.kind == Location.Kind.WORKSITE,
        ),
        projects=projects,
        allocations=PurchaseAllocation.objects.in_bulk(list(book.allocations)),
    )


# --- writing --------------------------------------------------------------------------


def _assign_number(receipt: GoodsReceipt, locks: LockOrder) -> None:
    """``REC-{company}-{branch}-{yyyy}-{00001}`` per branch (L9, last)."""
    owner, year = owner_code(receipt.branch), receipt.received_at.year
    locks.enter(LockLevel.DOCUMENT_SEQUENCE)
    value = next_number(f"receipt:{owner}:{year}")
    receipt.number = format_number(settings.RECEIPT_NUMBER_PREFIX, owner, year, value)


def _write_lines(
    receipt: GoodsReceipt, plans: Sequence[_LinePlan], facts: _Facts
) -> list[ReceiptAttribution]:
    attributions: list[ReceiptAttribution] = []
    for plan in plans:
        line = GoodsReceiptLine.objects.create(
            receipt=receipt,
            po=receipt.po,
            po_line=plan.po_line,
            qty_received=plan.item.qty_received,
            qty_rejected=plan.item.qty_rejected,
            issue=plan.item.issue,
            issue_note=plan.item.issue_note.strip(),
            surplus_qty=plan.surplus,
        )
        attributions.extend(
            ReceiptAttribution.objects.create(
                receipt_line=line,
                allocation=facts.allocations[allocation_id],
                po_line=plan.po_line,
                qty=quantity,
            )
            for allocation_id, quantity in plan.split.items()
        )
    return attributions


def _line_data(plan: _LinePlan, facts: _Facts) -> dict[str, Any]:
    return {
        "po_line_id": plan.po_line.pk,
        "line_no": plan.po_line.line_no,
        "received": plan.item.qty_received,
        "rejected": plan.item.qty_rejected,
        "issue": plan.item.issue,
        "issue_note": plan.item.issue_note.strip(),
        "surplus": plan.surplus,
        "attributions": [
            {
                "allocation_id": allocation_id,
                "request_line_id": facts.allocations[allocation_id].request_line_id,
                "qty": quantity,
            }
            for allocation_id, quantity in plan.split.items()
        ],
    }


def _record(principal: Principal, action: str, receipt: GoodsReceipt, **data: Any) -> None:
    record(
        principal.audit_identity,
        action,
        obj=receipt,
        branch_id=receipt.branch_id,
        data={"number": receipt.number, "order_id": receipt.po_id, **data},
    )
    events.publish(action, receipt_id=receipt.pk, po_id=receipt.po_id)


def _settle(
    principal: Principal,
    order: PurchaseOrder,
    component: Component,
    request_line_ids: Collection[UUID],
    cause: str,
) -> None:
    """Automatic close and reopen of the requests and the order (§4.9), then reconcile."""
    requests = MaterialRequest.objects.filter(lines__pk__in=list(request_line_ids)).distinct()
    for request in requests.order_by("pk"):
        _settle_closure(principal, request, list(request.lines.all()))
    _settle_order(principal, order, component)
    component.reconcile(principal, cause)


# --- services -------------------------------------------------------------------------


@transactional
def post_receipt(
    principal: Principal,
    po_id: UUID,
    *,
    location_id: UUID,
    lines: Sequence[ReceiptLineInput],
    delivery_note_ref: str = "",
    notes: str = "",
    idempotency_key: str | None = None,
) -> GoodsReceipt:
    """Record goods of an issued order that arrived at ``location`` (a warehouse or a
    worksite), attribute them and, at a worksite, hand them over at once.

    Over-receipt is refused (I4). With an idempotency key, a repeated post (a double tap)
    returns the receipt again.
    """
    if not lines:
        raise ValidationError(_("Enter at least one line."), code="empty")
    if len({item.po_line_id for item in lines}) != len(lines):
        raise ValueError("Each order line is received once per receipt")
    locks = LockOrder()
    order = PurchaseOrder.objects.get(pk=po_id)
    location = _location(location_id)
    actor = _authorize_post(principal, order, location, locks)

    def execute() -> GoodsReceipt:
        component = Component.lock(locks, orders=[order.pk])
        locked = PurchaseOrder.objects.select_related("branch__company").get(pk=order.pk)
        # Read again after L0: a project or branch closing now waits for this transaction.
        here = _location(location_id)
        _check_order(locked)
        _check_location(locked, here)
        facts = _facts(component, here)
        plans = [_plan_line(locked, here, facts, item) for item in lines]
        # Capacity last, once every line and split is valid: otherwise which refusal comes
        # back would tell a site manager whether a quantity fits the whole order line.
        for plan in plans:
            _check_capacity(plan.item, plan.po_line, facts.book)
        receipt = GoodsReceipt(
            po=locked,
            branch=locked.branch,
            location=here,
            received_at=db_now(),
            received_by=actor,
            delivery_note_ref=delivery_note_ref.strip(),
            notes=notes.strip(),
        )
        _assign_number(receipt, locks)
        receipt.save()
        attributions = _write_lines(receipt, plans, facts)
        _record(
            principal,
            "receipt.posted",
            receipt,
            order=locked.number,
            location_id=here.pk,
            ship_to_id=locked.ship_to_id,
            location_differs=here.pk != locked.ship_to_id,
            delivery_note_ref=receipt.delivery_note_ref,
            lines=[_line_data(plan, facts) for plan in plans],
        )
        if here.project is not None:
            direct.post_for_receipt(principal, actor, receipt, here.project, attributions, locks)
        attributed = {facts.allocations[a.allocation_id].request_line_id for a in attributions}
        _settle(principal, locked, component, attributed, "receipt.post")
        return receipt

    return execute_once(
        actor_id=principal.user_id,
        key=idempotency_key,
        operation="receipt.post",
        payload={
            "po_id": po_id,
            "location_id": location_id,
            "delivery_note_ref": delivery_note_ref.strip(),
            "notes": notes.strip(),
            "lines": [item.payload() for item in lines],
        },
        execute=execute,
        replay=lambda _type, pk: GoodsReceipt.objects.get(pk=pk),
    )


@transactional
def void_receipt(principal: Principal, receipt_id: UUID, *, reason: str) -> GoodsReceipt:
    """Void a posted receipt (a reason is required) and its direct handover.

    Its attributions stop counting; the order and the requests reopen when eligible and the
    revived pending is reconciled (§4.9). Correcting a receipt is a void and a new one.
    """
    reason = _reason(reason)
    locks = LockOrder()
    receipt = GoodsReceipt.objects.get(pk=receipt_id)
    actor = authorize_in_txn(principal, VOID, receipt, locks=locks, read=VIEW)
    attributed = set(
        ReceiptAttribution.objects.filter(receipt_line__receipt=receipt).values_list(
            "allocation__request_line_id", flat=True
        )
    )
    component = Component.lock(locks, orders=[receipt.po_id], request_lines=attributed)
    locks.lock(LockLevel.RECEIPTS, GoodsReceipt, [receipt.pk])
    locked = GoodsReceipt.objects.select_related("branch__company", "po").get(pk=receipt.pk)
    left = transition(locked, Status.VOIDED, "void")
    now = db_now()
    locked.voided_at, locked.voided_by, locked.void_reason = now, actor, reason
    locked.save()
    _record(principal, "receipt.voided", locked, from_status=left, reason=reason)
    direct.void_for_receipt(principal, actor, locked, locks, reason=reason, now=now)
    _settle(principal, locked.po, component, attributed, "receipt.void")
    return locked
