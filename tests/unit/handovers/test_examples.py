"""M6.2's done-when examples, through the services with real orders, receipts and handovers.

- the warehouse example: 20 received at the warehouse, 12 handed over, 8 outstanding;
- a handover from stock on a line with pending receipts: D rises above R, so committed
  max(D, R) + P + Q passes A and the reconciliation cuts the pending above what is still
  needed (an audited ``EXCESS_COMMITMENT`` trim; the order line shows the freed quantity
  as *unallocated on order*);
- a handover's void reopens what it closed and never revives a trimmed commitment.
"""

from decimal import Decimal
from typing import Any

import pytest

from apps.audit.models import AuditEvent
from apps.handovers import services
from apps.material_requests.models import MaterialRequest, MaterialRequestLine
from apps.purchasing.ledger import book_for_order_lines
from apps.purchasing.models import PurchaseOrder
from tests.unit.handovers.conftest import HandOver
from tests.unit.purchasing.conftest import Buy, MakeLine, allocation, ledger_of, only_line, q
from tests.unit.receiving.conftest import Receive

pytestmark = pytest.mark.django_db

D = Decimal


def figures(line: MaterialRequestLine) -> tuple[Decimal, ...]:
    """(R, P, D, committed, awaiting handover, outstanding, to purchase)."""
    entry = ledger_of(line)
    f = entry.flows
    return (
        f.received,
        f.pending,
        f.delivered,
        entry.committed,
        entry.awaiting_handover,
        entry.outstanding,
        entry.to_purchase,
    )


def unallocated(order: PurchaseOrder) -> Decimal:
    line = only_line(order)
    return book_for_order_lines([line.pk]).order_line_totals(line.pk).unallocated


def trims() -> list[tuple[str, str, str]]:
    return [
        (event.data["from"], event.data["to"], event.data["cause"])
        for event in AuditEvent.objects.filter(action="purchase_allocation.trimmed").order_by(
            "occurred_at", "id"
        )
        if event.data["reason"] == "EXCESS_COMMITMENT"
    ]


def status(line: MaterialRequestLine) -> str:
    return MaterialRequest.objects.get(pk=line.request_id).status


def test_the_warehouse_example(
    make_line: MakeLine, buy: Buy, receive: Receive, hand_over: HandOver
) -> None:
    """20 received at the warehouse, 12 handed over, 8 outstanding."""
    line = make_line("20")
    receive(buy((line, "20")), "20")
    assert figures(line) == q(20, 0, 0, 20, 20, 20, 0)
    hand_over((line, "12"))
    assert figures(line) == q(20, 0, 12, 20, 8, 8, 0)
    assert status(line) == MaterialRequest.Status.APPROVED
    assert not trims()  # max(D, R) stays 20: nothing to cut
    hand_over((line, "8"))
    assert status(line) == MaterialRequest.Status.CLOSED
    assert figures(line) == q(20, 0, 20, 0, 0, 0, 0)  # closed: nothing open is left


def test_a_stock_handover_cuts_the_excess_pending(
    make_line: MakeLine, buy: Buy, hand_over: HandOver
) -> None:
    """Approved 10, all 10 pending on an order; 4 handed over from stock: P is cut to 6."""
    line = make_line("10")
    order = buy((line, "10"))
    assert figures(line) == q(0, 10, 0, 10, 0, 10, 0)
    hand_over((line, "4"))
    assert figures(line) == q(0, 6, 4, 10, 0, 6, 0)
    assert allocation(order, line).qty == 6
    assert trims() == [("10.000", "6.000", "handover.post")]
    assert unallocated(order) == 4  # flagged on the purchasing worklist


def test_a_line_covered_from_stock_loses_its_pending(
    make_line: MakeLine, buy: Buy, hand_over: HandOver
) -> None:
    """A line handed over in full while its request stays open: its pending is cut to 0."""
    line = make_line("10")
    MaterialRequestLine.objects.create(
        request_id=line.request_id,
        project=line.project,
        line_no=2,
        material=line.material,
        unit=line.unit,
        requested_qty=D(5),
        approved_qty=D(5),
        decision=MaterialRequestLine.Decision.APPROVED,
        decided_by=line.decided_by,
        decided_at=line.decided_at,
        sourcing=MaterialRequestLine.Sourcing.STOCK,
    )
    order = buy((line, "10"))
    hand_over((line, "10"))
    assert status(line) == MaterialRequest.Status.APPROVED  # line 2 is still outstanding
    assert figures(line) == q(0, 0, 10, 10, 0, 0, 0)
    assert allocation(order, line).qty == 0
    assert trims() == [("10.000", "0.000", "handover.post")]
    assert unallocated(order) == 10


def test_a_request_closed_by_a_stock_handover_expects_nothing(
    mm: Any, make_line: MakeLine, buy: Buy, hand_over: HandOver
) -> None:
    """The request closes, so its lines are not open: P = 0 without a cut, the order line's
    remaining is unallocated (flagged). The void reopens it and the pending comes back
    within the approved quantity."""
    line = make_line("10")
    order = buy((line, "10"))
    handover = hand_over((line, "10"))
    assert status(line) == MaterialRequest.Status.CLOSED
    assert ledger_of(line).flows.pending == 0
    assert unallocated(order) == 10
    services.void_handover(mm, handover.pk, reason="Taken back to the warehouse")
    assert status(line) == MaterialRequest.Status.APPROVED
    assert figures(line) == q(0, 10, 0, 10, 0, 10, 0)
    assert not trims()


def test_a_void_never_revives_a_trimmed_commitment(
    mm: Any, make_line: MakeLine, buy: Buy, hand_over: HandOver
) -> None:
    """After the cut to 6, voiding the handover leaves P = 6 and 4 to purchase (like E5)."""
    line = make_line("10")
    order = buy((line, "10"))
    handover = hand_over((line, "4"))
    services.void_handover(mm, handover.pk, reason="Wrong request")
    assert figures(line) == q(0, 6, 0, 6, 0, 10, 4)
    assert allocation(order, line).qty == 6


def test_received_goods_handed_over_cut_nothing(
    make_line: MakeLine, buy: Buy, receive: Receive, hand_over: HandOver
) -> None:
    """Handovers consume the received quantity first: D ≤ R changes no commitment."""
    line = make_line("10")
    order = buy((line, "10"))
    receive(order, "6")
    assert figures(line) == q(6, 4, 0, 10, 6, 10, 0)
    hand_over((line, "6"))
    assert figures(line) == q(6, 4, 6, 10, 0, 4, 0)
    assert not trims()
    hand_over((line, "1"))  # one more from stock: the pending above the need is cut
    assert figures(line) == q(6, 3, 7, 10, 0, 3, 0)
    assert trims() == [("10.000", "9.000", "handover.post")]


def test_the_cut_reaches_other_requests_through_the_waterfall(
    make_line: MakeLine, buy: Buy, receive: Receive, hand_over: HandOver
) -> None:
    """Only the line handed over is over-committed; the others' pending is untouched."""
    first, second = make_line("10"), make_line("10")
    order = buy((first, "10"), (second, "10"))
    hand_over((first, "5"))
    assert (allocation(order, first).qty, allocation(order, second).qty) == (D(5), D(10))
    assert figures(second) == q(0, 10, 0, 10, 0, 10, 0)
    receive(order, "8")  # the waterfall: 5 to the first, 3 to the second
    assert ledger_of(first).flows.received == 5
    assert ledger_of(second).flows.received == 3
