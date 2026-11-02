"""The allocation component and its reconciliation (plan §4.5, §4.7; F40, F51, F57).

A request line's pending depends on the other allocations of its order lines (the
waterfall), so one operation can raise the commitment of requests it never touched (E10).
Every operation that changes a reconciliation input therefore works on its **component**:
the request lines and order lines connected through live allocations (``qty > 0``, order
not cancelled), starting from the rows the operation changes (the *seeds*; a header seed
brings all its lines).

1. **Discover** the component from the seeds, following allocations both ways. All
   allocations share one procurement branch (C5), so a component never leaves a branch.
   Above ``MAX_NODES`` request and order lines the operation is refused.
2. **Lock** every request header (L2) and line (L3), then every order header (L4) and line
   (L5), each level in id order and before anything above L5; then discover again under
   the locks and retry the transaction if the component changed. The pending P of every
   allocation is snapshotted here, before the operation.
3. The service **applies** its operation (it may trim allocations: ``trim``,
   ``trim_to_attributed``).
4. **Fixpoint** (``reconcile``): while an open request line has committed > A, cut its excess
   from one allocation at a time, in this order: draft reservations, newest first;
   pending allocations whose P rose since the snapshot; other pending allocations, newest
   first. A cut targets the effective pending: to remove x from an allocation with pending
   P, qty := attributed + (P - min(x, P)); a reservation loses min(x, Q). The waterfall is
   recomputed after each cut (it may hand quantity to the next allocation of the line), so
   the loop ends only when every open line holds; it terminates because each cut lowers
   Σ qty. Over-committed lines are taken in attribution order.
5. **Record** each allocation the fixpoint lowered as one audited trim (``EXCESS_COMMITMENT``).
   The freed quantity shows as *unallocated on order* on its line, which the worklist flags.

Issue revalidation (F38) runs steps 1-3 and refuses, with the list of lines, instead of
cutting. Writes to allocations happen only here and in the services, under the locks of
both parents.
"""

from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.access.principals import Principal
from apps.audit.recorder import record
from apps.core import events
from apps.core.locking import LockLevel, LockOrder, LockSetChanged
from apps.material_requests import ledger as request_ledger
from apps.material_requests.models import MaterialRequest, MaterialRequestLine
from apps.purchasing import receipts
from apps.purchasing.ledger import LIVE, book_for_nodes
from apps.purchasing.models import PurchaseAllocation, PurchaseOrder, PurchaseOrderLine
from apps.purchasing.waterfall import ZERO, AllocationFacts, Book, RequestLineFacts

MAX_NODES = 200  # request lines + order lines of one component (§4.5, step 1)
MAX_CUTS = 10_000  # far above any real component; reaching it is a programming error


class TrimReason(StrEnum):
    """Why an allocation's ceiling was lowered (plan §4.5, "Changing an allocation")."""

    EXCESS_COMMITMENT = "EXCESS_COMMITMENT"  # the reconciliation fixpoint
    REQUEST_LINE_CLOSED = "REQUEST_LINE_CLOSED"  # a request line closed short
    REQUEST_CLOSED = "REQUEST_CLOSED"  # a request closed manually
    ORDER_LINE_CLOSED = "ORDER_LINE_CLOSED"  # an order line closed short
    ORDER_CANCELLED = "ORDER_CANCELLED"
    DRAFT_DISCARDED = "DRAFT_DISCARDED"
    ORDERED_QTY_REDUCED = "ORDERED_QTY_REDUCED"  # an ordered quantity lowered
    UNALLOCATED = "UNALLOCATED"  # purchasing unallocated explicitly


class ComponentTooLarge(ValidationError):
    def __init__(self) -> None:
        super().__init__(
            _(
                "This change reaches more than %(limit)s linked order and request lines. "
                "Split the work across separate orders, or ask an administrator."
            ),
            code="component_too_large",
            params={"limit": MAX_NODES},
        )


@dataclass(frozen=True)
class Seeds:
    requests: frozenset[UUID] = frozenset()
    request_lines: frozenset[UUID] = frozenset()
    orders: frozenset[UUID] = frozenset()
    order_lines: frozenset[UUID] = frozenset()


@dataclass(frozen=True)
class Nodes:
    request_ids: frozenset[UUID]
    request_line_ids: frozenset[UUID]
    order_ids: frozenset[UUID]
    order_line_ids: frozenset[UUID]


def _check_bound(request_lines: set[UUID], order_lines: set[UUID]) -> None:
    if len(request_lines) + len(order_lines) > MAX_NODES:
        raise ComponentTooLarge


def discover(seeds: Seeds) -> Nodes:
    """The component of ``seeds`` (no locks): breadth first through live allocations."""
    request_lines = set(seeds.request_lines) | set(
        MaterialRequestLine.objects.filter(request_id__in=seeds.requests).values_list(
            "pk", flat=True
        )
    )
    order_lines = set(seeds.order_lines) | set(
        PurchaseOrderLine.objects.filter(po_id__in=seeds.orders).values_list("pk", flat=True)
    )
    frontier_requests, frontier_orders = set(request_lines), set(order_lines)
    while frontier_requests or frontier_orders:
        _check_bound(request_lines, order_lines)
        edges = (
            PurchaseAllocation.objects.filter(LIVE)
            .filter(Q(request_line_id__in=frontier_requests) | Q(po_line_id__in=frontier_orders))
            .values_list("request_line_id", "po_line_id")
        )
        frontier_requests = {line for line, _ in edges} - request_lines
        frontier_orders = {line for _, line in edges} - order_lines
        request_lines |= frontier_requests
        order_lines |= frontier_orders
    _check_bound(request_lines, order_lines)
    return Nodes(
        request_ids=frozenset(seeds.requests)
        | frozenset(
            MaterialRequestLine.objects.filter(pk__in=request_lines).values_list(
                "request_id", flat=True
            )
        ),
        request_line_ids=frozenset(request_lines),
        order_ids=frozenset(seeds.orders)
        | frozenset(
            PurchaseOrderLine.objects.filter(pk__in=order_lines).values_list("po_id", flat=True)
        ),
        order_line_ids=frozenset(order_lines),
    )


@dataclass(frozen=True)
class Trim:
    allocation_id: UUID
    before: Decimal
    after: Decimal
    reason: TrimReason


def over_committed(book: Book, delivered: Mapping[UUID, Decimal]) -> list[RequestLineFacts]:
    """Open lines with committed > A, in attribution order."""
    over = [
        line
        for line in book.request_lines.values()
        if line.is_open and book.committed(line.id, delivered.get(line.id, ZERO)) > line.approved
    ]
    return sorted(over, key=lambda line: (line.order_key, line.id))


def _cut_candidate(
    book: Book, line_id: UUID, before: Mapping[UUID, Decimal]
) -> AllocationFacts | None:
    newest_first = sorted(book.of_request_line(line_id), key=lambda a: a.id, reverse=True)
    for allocation in newest_first:
        if book.reserved(allocation.id) > 0:
            return allocation
    for allocation in newest_first:
        if book.pending(allocation.id) > before.get(allocation.id, ZERO):
            return allocation
    for allocation in newest_first:
        if book.pending(allocation.id) > 0:
            return allocation
    return None


def _inconsistent() -> ValidationError:
    return ValidationError(
        _(
            "The quantities of a request line are inconsistent (more delivered or received "
            "than approved); ask an administrator to check it."
        ),
        code="integrity",
    )


def fixpoint(
    book: Book, delivered: Mapping[UUID, Decimal], before: Mapping[UUID, Decimal]
) -> dict[UUID, Decimal]:
    """The new ``qty`` of every allocation the cuts lowered (step 4 above)."""
    cut: dict[UUID, Decimal] = {}
    current = book
    for _attempt in range(MAX_CUTS):
        over = over_committed(current, delivered)
        if not over:
            return cut
        line = over[0]
        excess = current.committed(line.id, delivered.get(line.id, ZERO)) - line.approved
        allocation = _cut_candidate(current, line.id, before)
        if allocation is None:
            raise _inconsistent()
        reserved = current.reserved(allocation.id)
        if reserved > 0:
            quantity = allocation.qty - min(excess, reserved)
        else:
            pending = current.pending(allocation.id)
            quantity = allocation.attributed + (pending - min(excess, pending))
        cut[allocation.id] = quantity
        current = current.with_quantities({allocation.id: quantity})
    raise RuntimeError("The commitment fixpoint did not converge")


class Component:
    """A locked component (``Component.lock``); see the module docstring."""

    def __init__(self, locks: LockOrder, seeds: Seeds, nodes: Nodes) -> None:
        self.locks = locks
        self.seeds = seeds
        self.nodes = nodes
        self._created: set[UUID] = set()
        self._before: dict[UUID, Decimal] = {}

    @classmethod
    def lock(
        cls,
        locks: LockOrder,
        *,
        requests: Iterable[UUID] = (),
        request_lines: Iterable[UUID] = (),
        orders: Iterable[UUID] = (),
        order_lines: Iterable[UUID] = (),
        between_orders_and_lines: Callable[[LockOrder], Any] | None = None,
    ) -> Component:
        """Discover, lock L2-L5 and verify (steps 1-2); snapshot every allocation's P.

        ``between_orders_and_lines`` takes the locks of level L4b (an order's dispatches).
        """
        seeds = Seeds(
            frozenset(requests), frozenset(request_lines), frozenset(orders), frozenset(order_lines)
        )
        nodes = discover(seeds)
        locks.lock(LockLevel.REQUEST_HEADERS, MaterialRequest, nodes.request_ids)
        locks.lock(LockLevel.REQUEST_LINES, MaterialRequestLine, nodes.request_line_ids)
        locks.lock(LockLevel.PO_HEADERS, PurchaseOrder, nodes.order_ids)
        if between_orders_and_lines is not None:
            between_orders_and_lines(locks)
        locks.lock(LockLevel.PO_LINES, PurchaseOrderLine, nodes.order_line_ids)
        if discover(seeds) != nodes:
            raise LockSetChanged("The allocation component changed while it was being locked")
        component = cls(locks, seeds, nodes)
        book = component.book()
        component._before = {pk: book.pending(pk) for pk in book.allocations}
        return component

    # --- reading ------------------------------------------------------------------------

    def add_order_line(self, line_id: UUID) -> None:
        """An order line the operation created under its (locked) order header."""
        self._created.add(line_id)

    @property
    def order_line_ids(self) -> frozenset[UUID]:
        return self.nodes.order_line_ids | self._created

    def book(self) -> Book:
        """The component's rows as they are now (under its locks)."""
        book = book_for_nodes(self.order_line_ids, self.nodes.request_line_ids)
        for allocation in book.allocations.values():
            if (
                allocation.request_line_id not in self.nodes.request_line_ids
                or allocation.po_line_id not in self.order_line_ids
            ):
                raise RuntimeError("An allocation reaches outside the locked component")
        return book

    def delivered(self, book: Book) -> dict[UUID, Decimal]:
        """D of the component's request lines (posted handovers of both kinds)."""
        found = request_ledger.flows(list(book.request_lines))
        return {line_id: flow.delivered for line_id, flow in found.items()}

    # --- writing ------------------------------------------------------------------------

    def trim(
        self,
        principal: Principal,
        allocation: PurchaseAllocation,
        to: Decimal,
        reason: TrimReason,
        cause: str,
        *,
        attributed: Decimal | None = None,
    ) -> Trim | None:
        """Lower one allocation to ``to``, never below what is attributed; audited.

        ``allocation`` is the row as read under the component's locks; ``attributed`` is
        what is attributed to it, when the caller already read it under those locks.
        """
        if allocation.request_line_id not in self.nodes.request_line_ids or (
            allocation.po_line_id not in self.order_line_ids
        ):
            raise RuntimeError("An allocation outside the locked component cannot be trimmed")
        before = allocation.qty
        if to >= before:
            return None
        if attributed is None:
            attributed = receipts.attributed([allocation.pk])[allocation.pk]
        if to < attributed:
            raise ValidationError(
                {
                    "qty": ValidationError(
                        _("%(attributed)s have already been received for this allocation."),
                        code="below_attributed",
                        params={"attributed": f"{attributed.normalize():f}"},
                    )
                }
            )
        PurchaseAllocation.objects.filter(pk=allocation.pk).update(
            qty=to, updated_at=timezone.now()
        )
        allocation.qty = to
        trim = Trim(allocation.pk, before, to, reason)
        _record_trim(principal, trim, cause)
        return trim

    def trim_to_attributed(
        self,
        principal: Principal,
        *,
        request_lines: Collection[UUID] = (),
        order_lines: Collection[UUID] = (),
        reason: TrimReason,
        cause: str,
    ) -> list[Trim]:
        """Every allocation of these lines down to what is attributed (closures, §4.5)."""
        wanted = PurchaseAllocation.objects.filter(LIVE).filter(
            Q(request_line_id__in=list(request_lines)) | Q(po_line_id__in=list(order_lines))
        )
        listed = list(wanted.order_by("pk"))
        attributed = receipts.attributed([allocation.pk for allocation in listed])
        trims = [
            self.trim(
                principal,
                allocation,
                attributed[allocation.pk],
                reason,
                cause,
                attributed=attributed[allocation.pk],
            )
            for allocation in listed
        ]
        return [trim for trim in trims if trim is not None]

    def allocated_lines(self, line_ids: Collection[UUID]) -> set[UUID]:
        """For the request services: which of these lines have a live allocation."""
        wanted = set(line_ids)
        return {
            allocation.request_line_id
            for allocation in self.book().allocations.values()
            if allocation.request_line_id in wanted
        }

    def trim_request_lines(
        self, principal: Principal, line_ids: Collection[UUID], *, reason: str, cause: str
    ) -> None:
        """For the request services (``material_requests.commitments``): closures."""
        self.trim_to_attributed(
            principal, request_lines=line_ids, reason=TrimReason(reason), cause=cause
        )

    def reconcile(self, principal: Principal, cause: str) -> list[Trim]:
        """Steps 4-5: cut every excess commitment in the component; record the trims."""
        book = self.book()
        cut = fixpoint(book, self.delivered(book), self._before)
        trims = []
        for allocation_id in sorted(cut):
            before = book.allocations[allocation_id].qty
            PurchaseAllocation.objects.filter(pk=allocation_id).update(
                qty=cut[allocation_id], updated_at=timezone.now()
            )
            trim = Trim(allocation_id, before, cut[allocation_id], TrimReason.EXCESS_COMMITMENT)
            _record_trim(principal, trim, cause)
            trims.append(trim)
        return trims


def _record_trim(principal: Principal, trim: Trim, cause: str) -> None:
    allocation = PurchaseAllocation.objects.select_related(
        "po_line__po", "request_line__request"
    ).get(pk=trim.allocation_id)
    order, request = allocation.po_line.po, allocation.request_line.request
    record(
        principal.audit_identity,
        "purchase_allocation.trimmed",
        obj=allocation,
        branch_id=order.branch_id,
        data={
            "order_id": order.pk,
            "order": order.number,
            "order_line": allocation.po_line.line_no,
            "request_id": request.pk,
            "request": request.number,
            "request_line": allocation.request_line.line_no,
            "from": trim.before,
            "to": trim.after,
            "reason": trim.reason,
            "cause": cause,
        },
    )
    events.publish(
        "purchase_allocation.trimmed",
        allocation_id=allocation.pk,
        po_id=order.pk,
        request_line_id=allocation.request_line_id,
        reason=str(trim.reason),
    )


def lock_for_requests(locks: LockOrder, request_ids: Collection[UUID]) -> Component:
    """The ``ComponentLocker`` purchasing registers with ``material_requests.commitments``."""
    return Component.lock(locks, requests=request_ids)
