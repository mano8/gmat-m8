"""The pending waterfall and the derived quantities of purchasing (plan §4.5).

Pure computation over facts that ``ledger`` loads, so the selectors, the request-line flow
source and the reconciliation fixpoint all compute the same numbers. Nothing is stored.

- **Attribution order** (F65): request priority (highest first), needed-by date (undated
  last), submission time, allocation id.
- **Draft reservation Q**: an allocation's ``qty`` while its order is ``DRAFT`` and its
  request line is open; 0 otherwise. Two drafts can never reserve the same quantity (C5).
- **Pending P** (F39): a *receivable* line (order ``ISSUED``, not closed short, remaining
  > 0) hands its remaining quantity to its allocations whose request line is open, in
  attribution order; each takes min(its ceiling remaining, what is left). Every other
  allocation has P = 0, so Σ pending of a line ≤ its remaining and *unallocated on order*
  is never negative (I12).

The plan suggests a window function for the waterfall; this is the same cumulative rule
in Python over the rows of a component or a page, which the fixpoint has to recompute
after every cut anyway.
"""

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

ZERO = Decimal("0")
DRAFT, ISSUED = "DRAFT", "ISSUED"
COMMITTING = frozenset({DRAFT, ISSUED})  # orders whose allocations count as allocated
PRIORITY_RANK = {"URGENT": 0, "HIGH": 1, "NORMAL": 2, "LOW": 3}
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class RequestLineFacts:
    """What the waterfall needs to know about a request line and its request."""

    id: UUID
    request_id: UUID
    approved: Decimal  # A (0 until approved)
    is_open: bool  # live, and the request is APPROVED
    priority: str
    needed_by: date | None
    submitted_at: datetime | None

    @property
    def order_key(self) -> tuple[int, bool, date, bool, datetime]:
        """The request's place in the attribution order (the allocation id breaks ties)."""
        return (
            PRIORITY_RANK.get(self.priority, len(PRIORITY_RANK)),
            self.needed_by is None,
            self.needed_by or date.min,
            self.submitted_at is None,
            self.submitted_at or _EPOCH,
        )


@dataclass(frozen=True)
class OrderLineFacts:
    id: UUID
    po_id: UUID
    order_status: str
    ordered: Decimal
    received: Decimal
    closed_short: bool

    @property
    def remaining(self) -> Decimal:
        """ordered - received (state-independent)."""
        return self.ordered - self.received

    @property
    def has_capacity(self) -> bool:
        """Not closed short and something left to receive (state-independent)."""
        return not self.closed_short and self.remaining > 0

    @property
    def receivable(self) -> bool:
        """The order is ``ISSUED`` and the line has capacity (current state)."""
        return self.order_status == ISSUED and self.has_capacity


@dataclass(frozen=True)
class AllocationFacts:
    id: UUID
    po_line_id: UUID
    request_line_id: UUID
    qty: Decimal
    attributed: Decimal

    @property
    def ceiling_remaining(self) -> Decimal:
        """qty - attributed (never below zero: I6 keeps attributed ≤ qty)."""
        return max(ZERO, self.qty - self.attributed)


@dataclass(frozen=True)
class RequestLineTotals:
    """What purchasing contributes to one request line (``material_requests.ledger``)."""

    received: Decimal = ZERO  # R: attributed over every order
    pending: Decimal = ZERO  # P
    reserved: Decimal = ZERO  # Q
    allocated: Decimal = ZERO  # Σ qty on draft or issued orders


@dataclass(frozen=True)
class OrderLineTotals:
    """The ledger of one order line (plan §4.5, "Per purchase-order line")."""

    ordered: Decimal
    received: Decimal
    remaining: Decimal
    allocated: Decimal  # allocated ceiling: Σ qty
    pending: Decimal
    reserved: Decimal
    unallocated: Decimal  # receivable ? remaining - pending : 0 (flagged when > 0)
    outstanding: Decimal  # receivable ? remaining : 0
    has_capacity: bool
    receivable: bool


class Book:
    """Order lines, request lines and the allocations between them, with P and Q computed.

    Every allocation's two lines must be in the book. ``with_quantities`` gives the book
    after some allocations change, as the fixpoint needs after each cut.
    """

    def __init__(
        self,
        order_lines: Iterable[OrderLineFacts],
        request_lines: Iterable[RequestLineFacts],
        allocations: Iterable[AllocationFacts],
    ) -> None:
        self.order_lines = {line.id: line for line in order_lines}
        self.request_lines = {line.id: line for line in request_lines}
        self.allocations = {allocation.id: allocation for allocation in allocations}
        self._by_order_line: defaultdict[UUID, list[AllocationFacts]] = defaultdict(list)
        self._by_request_line: defaultdict[UUID, list[AllocationFacts]] = defaultdict(list)
        for allocation in self.allocations.values():
            self._by_order_line[allocation.po_line_id].append(allocation)
            self._by_request_line[allocation.request_line_id].append(allocation)
        for listed in self._by_order_line.values():
            listed.sort(key=self.attribution_key)
        for listed in self._by_request_line.values():
            listed.sort(key=lambda allocation: allocation.id)
        self._pending: dict[UUID, Decimal] = {}
        self._reserved: dict[UUID, Decimal] = {}
        for line_id, listed in self._by_order_line.items():
            self._compute(self.order_lines[line_id], listed)

    def _compute(self, line: OrderLineFacts, listed: list[AllocationFacts]) -> None:
        left = line.remaining if line.receivable else ZERO
        for allocation in listed:
            is_open = self.request_lines[allocation.request_line_id].is_open
            take = min(allocation.ceiling_remaining, left) if line.receivable and is_open else ZERO
            left -= take
            self._pending[allocation.id] = take
            draft = line.order_status == DRAFT and is_open
            self._reserved[allocation.id] = allocation.qty if draft else ZERO

    def attribution_key(self, allocation: AllocationFacts) -> tuple[object, ...]:
        return (*self.request_lines[allocation.request_line_id].order_key, allocation.id)

    def with_quantities(self, quantities: Mapping[UUID, Decimal]) -> Book:
        """The same book with these allocations' ``qty`` replaced."""
        return Book(
            self.order_lines.values(),
            self.request_lines.values(),
            (
                replace(allocation, qty=quantities[allocation.id])
                if allocation.id in quantities
                else allocation
                for allocation in self.allocations.values()
            ),
        )

    def with_order_status(self, po_id: UUID, status: str) -> Book:
        """The same book with order ``po_id`` in ``status`` (issue revalidation, F38)."""
        return Book(
            (
                replace(line, order_status=status) if line.po_id == po_id else line
                for line in self.order_lines.values()
            ),
            self.request_lines.values(),
            self.allocations.values(),
        )

    # --- per allocation -----------------------------------------------------------------

    def pending(self, allocation_id: UUID) -> Decimal:
        return self._pending[allocation_id]

    def reserved(self, allocation_id: UUID) -> Decimal:
        return self._reserved[allocation_id]

    def of_order_line(self, line_id: UUID) -> list[AllocationFacts]:
        """The line's allocations in attribution order."""
        return list(self._by_order_line.get(line_id, ()))

    def of_request_line(self, line_id: UUID) -> list[AllocationFacts]:
        """The request line's allocations, oldest first."""
        return list(self._by_request_line.get(line_id, ()))

    # --- per line -----------------------------------------------------------------------

    def request_line_totals(self, line_id: UUID) -> RequestLineTotals:
        listed = self._by_request_line.get(line_id, ())
        return RequestLineTotals(
            received=sum((a.attributed for a in listed), ZERO),
            pending=sum((self._pending[a.id] for a in listed), ZERO),
            reserved=sum((self._reserved[a.id] for a in listed), ZERO),
            allocated=sum(
                (
                    a.qty
                    for a in listed
                    if self.order_lines[a.po_line_id].order_status in COMMITTING
                ),
                ZERO,
            ),
        )

    def committed(self, line_id: UUID, delivered: Decimal) -> Decimal:
        """open ? max(D, R) + P + Q : 0 (plan §4.5)."""
        if not self.request_lines[line_id].is_open:
            return ZERO
        totals = self.request_line_totals(line_id)
        return max(delivered, totals.received) + totals.pending + totals.reserved

    def order_line_totals(self, line_id: UUID) -> OrderLineTotals:
        line = self.order_lines[line_id]
        listed = self._by_order_line.get(line_id, ())
        pending = sum((self._pending[a.id] for a in listed), ZERO)
        return OrderLineTotals(
            ordered=line.ordered,
            received=line.received,
            remaining=line.remaining,
            allocated=sum((a.qty for a in listed), ZERO),
            pending=pending,
            reserved=sum((self._reserved[a.id] for a in listed), ZERO),
            unallocated=line.remaining - pending if line.receivable else ZERO,
            outstanding=line.remaining if line.receivable else ZERO,
            has_capacity=line.has_capacity,
            receivable=line.receivable,
        )
