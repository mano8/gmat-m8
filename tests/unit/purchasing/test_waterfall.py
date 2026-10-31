"""The pending waterfall and the derived quantities, as pure computation (plan §4.5).

The attribution order, P and Q per allocation, the per-line totals, and I12 as a property
over random books: pending only on receivable lines of open request lines, Σ pending of a
line ≤ its remaining, each pending ≤ its ceiling remaining, reservations only on drafts.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from hypothesis import given, settings
from hypothesis import strategies as st

from apps.purchasing.waterfall import (
    AllocationFacts,
    Book,
    OrderLineFacts,
    RequestLineFacts,
)

D = Decimal
T0 = datetime(2026, 10, 1, tzinfo=UTC)


def uid(number: int) -> UUID:
    return UUID(int=number)


def request_line(
    number: int,
    *,
    approved: str = "10",
    is_open: bool = True,
    priority: str = "NORMAL",
    needed_by: date | None = None,
    submitted: int = 0,
) -> RequestLineFacts:
    return RequestLineFacts(
        id=uid(1000 + number),
        request_id=uid(2000 + number),
        approved=D(approved),
        is_open=is_open,
        priority=priority,
        needed_by=needed_by,
        submitted_at=T0 + timedelta(minutes=submitted or number),
    )


def order_line(
    number: int,
    *,
    ordered: str = "30",
    received: str = "0",
    status: str = "ISSUED",
    closed: bool = False,
) -> OrderLineFacts:
    return OrderLineFacts(
        id=uid(3000 + number),
        po_id=uid(4000 + number),
        order_status=status,
        ordered=D(ordered),
        received=D(received),
        closed_short=closed,
    )


def alloc(
    number: int, line: OrderLineFacts, request: RequestLineFacts, qty: str, attributed: str = "0"
) -> AllocationFacts:
    return AllocationFacts(uid(number), line.id, request.id, D(qty), D(attributed))


class TestAttributionOrder:
    def test_priority_then_needed_by_then_submission_then_allocation(self) -> None:
        line = order_line(1, ordered="60")
        urgent = request_line(1, priority="URGENT", submitted=50)
        dated = request_line(2, needed_by=date(2026, 11, 1), submitted=40)
        early = request_line(3, submitted=1)
        late = request_line(4, submitted=2)
        undated_low = request_line(5, priority="LOW", submitted=0)
        same = request_line(6, submitted=2)  # ties with ``late``: allocation id decides
        book = Book(
            [line],
            [urgent, dated, early, late, undated_low, same],
            [
                alloc(9, line, same, "1"),
                alloc(1, line, undated_low, "1"),
                alloc(2, line, late, "1"),
                alloc(3, line, early, "1"),
                alloc(4, line, dated, "1"),
                alloc(5, line, urgent, "1"),
            ],
        )
        order = [a.request_line_id for a in book.of_order_line(line.id)]
        assert order == [urgent.id, dated.id, early.id, late.id, same.id, undated_low.id]

    def test_an_unknown_priority_comes_last(self) -> None:
        assert request_line(1, priority="?").order_key > request_line(2, priority="LOW").order_key


class TestPending:
    def test_the_remaining_quantity_flows_in_attribution_order(self) -> None:
        """E6: 30 received on a line of 45 allocated 20 / 15 / 10 → pending 0 / 5 / 10."""
        line = order_line(1, ordered="45", received="30")
        x, y, z = request_line(1), request_line(2), request_line(3)
        allocations = [
            alloc(1, line, x, "20", attributed="20"),
            alloc(2, line, y, "15", attributed="10"),
            alloc(3, line, z, "10"),
        ]
        book = Book([line], [x, y, z], allocations)
        assert [book.pending(a.id) for a in allocations] == [0, 5, 10]
        totals = book.order_line_totals(line.id)
        assert (totals.remaining, totals.pending, totals.unallocated, totals.outstanding) == (
            D(15),
            D(15),
            D(0),
            D(15),
        )

    def test_a_short_line_hands_out_what_it_has(self) -> None:
        """E7: 15 of 20 received at X's site (X 10 + 5 surplus): remaining 5 → P_Y 5."""
        line = order_line(1, ordered="20", received="15")
        x, y = request_line(1), request_line(2)
        allocations = [alloc(1, line, x, "10", attributed="10"), alloc(2, line, y, "10")]
        book = Book([line], [x, y], allocations)
        assert [book.pending(a.id) for a in allocations] == [0, 5]
        assert book.order_line_totals(line.id).unallocated == 0  # never negative

    def test_a_closed_request_line_takes_nothing(self) -> None:
        line = order_line(1, ordered="20")
        closed, other = request_line(1, is_open=False), request_line(2)
        allocations = [alloc(1, line, closed, "10"), alloc(2, line, other, "10")]
        book = Book([line], [closed, other], allocations)
        assert [book.pending(a.id) for a in allocations] == [0, 10]
        assert book.order_line_totals(line.id).unallocated == 10
        assert book.committed(closed.id, D(0)) == 0

    def test_lines_that_are_not_receivable_pend_nothing(self) -> None:
        r = request_line(1)
        lines = [
            order_line(1, closed=True),
            order_line(2, ordered="10", received="10"),
            order_line(3, status="CLOSED"),
            order_line(4, status="CANCELLED"),
            order_line(5, status="DRAFT"),
        ]
        allocations = [alloc(n, line, r, "10") for n, line in enumerate(lines, 1)]
        book = Book(lines, [r], allocations)
        assert {book.pending(a.id) for a in allocations} == {D(0)}
        assert [book.order_line_totals(line.id).receivable for line in lines] == [False] * 5
        assert [book.order_line_totals(line.id).has_capacity for line in lines] == [
            False,
            False,
            True,
            True,
            True,
        ]
        assert book.order_line_totals(lines[0].id).outstanding == 0


class TestReservations:
    def test_a_draft_reserves_its_allocations_for_open_lines(self) -> None:
        draft = order_line(1, status="DRAFT")
        r, closed = request_line(1), request_line(2, is_open=False)
        allocations = [alloc(1, draft, r, "7"), alloc(2, draft, closed, "3")]
        book = Book([draft], [r, closed], allocations)
        assert [book.reserved(a.id) for a in allocations] == [7, 0]
        assert book.order_line_totals(draft.id).reserved == 7


class TestTotals:
    def test_a_request_line_across_orders(self) -> None:
        r = request_line(1, approved="20")
        issued, draft = order_line(1, ordered="10", received="4"), order_line(2, status="DRAFT")
        closed, cancelled = order_line(3, status="CLOSED"), order_line(4, status="CANCELLED")
        book = Book(
            [issued, draft, closed, cancelled],
            [r],
            [
                alloc(1, issued, r, "10", attributed="4"),
                alloc(2, draft, r, "3"),
                alloc(3, closed, r, "2", attributed="2"),
                alloc(4, cancelled, r, "0"),
            ],
        )
        totals = book.request_line_totals(r.id)
        assert (totals.received, totals.pending, totals.reserved, totals.allocated) == (
            D(6),
            D(6),
            D(3),
            D(13),  # draft and issued orders only
        )
        assert book.committed(r.id, delivered=D(1)) == 15  # max(1, 6) + 6 + 3
        assert book.committed(r.id, delivered=D(9)) == 18  # max(9, 6) + 6 + 3

    def test_an_unknown_line_has_nothing(self) -> None:
        book = Book([], [request_line(1)], [])
        assert book.of_request_line(uid(1001)) == []
        assert book.request_line_totals(uid(1001)).allocated == 0


class TestVariants:
    def test_with_quantities_and_with_order_status(self) -> None:
        draft = order_line(1, status="DRAFT")
        r = request_line(1)
        allocation = alloc(1, draft, r, "10")
        book = Book([draft], [r], [allocation])
        assert book.with_quantities({allocation.id: D(4)}).reserved(allocation.id) == 4
        issued = book.with_order_status(draft.po_id, "ISSUED")
        assert (issued.reserved(allocation.id), issued.pending(allocation.id)) == (D(0), D(10))
        assert book.with_order_status(uid(1), "ISSUED").reserved(allocation.id) == 10


# --- I12 as a property -------------------------------------------------------------------

quantities = st.integers(min_value=0, max_value=40).map(D)


@st.composite
def books(draw: st.DrawFn) -> Book:
    requests = [
        request_line(
            number,
            is_open=draw(st.booleans()),
            priority=draw(st.sampled_from(["LOW", "NORMAL", "HIGH", "URGENT"])),
            needed_by=draw(st.none() | st.dates(date(2026, 1, 1), date(2026, 12, 31))),
        )
        for number in range(draw(st.integers(1, 5)))
    ]
    lines: list[OrderLineFacts] = []
    allocations: list[AllocationFacts] = []
    for number in range(draw(st.integers(1, 4))):
        ordered = draw(st.integers(1, 60))
        line = order_line(
            number,
            ordered=str(ordered),
            received=str(draw(st.integers(0, ordered))),
            status=draw(st.sampled_from(["DRAFT", "ISSUED", "CLOSED", "CANCELLED"])),
            closed=draw(st.booleans()),
        )
        lines.append(line)
        chosen = draw(st.lists(st.sampled_from(requests), unique=True, max_size=len(requests)))
        for request in chosen:
            qty = draw(quantities)
            attributed = draw(st.integers(0, int(qty)))
            allocations.append(
                AllocationFacts(uid(len(allocations) + 1), line.id, request.id, qty, D(attributed))
            )
    return Book(lines, requests, allocations)


@settings(max_examples=200, deadline=None)
@given(books())
def test_i12_holds_for_any_book(book: Book) -> None:
    for line in book.order_lines.values():
        listed = book.of_order_line(line.id)
        pending = [book.pending(a.id) for a in listed]
        assert all(p >= 0 for p in pending)
        assert sum(pending, D(0)) <= max(D(0), line.remaining)
        totals = book.order_line_totals(line.id)
        assert totals.unallocated >= 0
        for allocation, p in zip(listed, pending, strict=True):
            assert p <= allocation.ceiling_remaining
            if not line.receivable or not book.request_lines[allocation.request_line_id].is_open:
                assert p == 0
            if line.order_status != "DRAFT":
                assert book.reserved(allocation.id) == 0
