"""Purchasing's side of the ledger: the request-line flow source, order-line ledgers, the
receipts seam and the deactivation provider (plan §4.5, S9, F62)."""

from decimal import Decimal
from uuid import UUID

import pytest

from apps.access.principals import SessionPrincipal
from apps.material_requests import ledger as request_ledger
from apps.organizations import services as organizations
from apps.organizations.deactivation import DeactivationRefused, OpenDocument, open_documents
from apps.purchasing import receipts, services
from apps.purchasing.ledger import book_for_order_lines, request_line_flows
from apps.purchasing.models import PurchaseOrder
from apps.purchasing.receipts import LineReceipts
from tests.org import Org, People
from tests.support import principal_for
from tests.unit.purchasing.conftest import Buy, MakeLine, allocation, flows, only_line, q

pytestmark = pytest.mark.django_db

D = Decimal


class TestFlows:
    def test_drafts_reserve_issued_orders_pend_and_cancelled_ones_count_nothing(
        self, mm: SessionPrincipal, buy: Buy, make_line: MakeLine
    ) -> None:
        line = make_line("10")
        draft = buy((line, "3"), issue=False)
        issued = buy((line, "4"))
        found = flows(line)
        assert (found.reserved, found.pending, found.allocated) == q(3, 4, 7)
        services.cancel(mm, issued.pk, reason="Wrong supplier")
        services.cancel(mm, draft.pk)
        assert flows(line) == request_ledger.Flows()
        assert request_line_flows([]) == {}
        assert request_line_flows([line.pk]) == {}  # trimmed to zero: nothing left to add

    def test_a_closed_order_keeps_what_was_received_out_of_allocated(
        self, mm: SessionPrincipal, buy: Buy, make_line: MakeLine
    ) -> None:
        line = make_line("10")
        order = buy((line, "10"))
        services.close_line_short(mm, only_line(order).pk, reason="Nothing will come")
        assert PurchaseOrder.objects.get(pk=order.pk).status == "CLOSED"
        assert flows(line) == request_ledger.Flows()  # trimmed to what was received: 0

    def test_lines_without_allocations_are_left_to_other_sources(self) -> None:
        assert request_line_flows([UUID(int=7)]) == {}


class TestOrderLines:
    def test_trimmed_allocations_on_request(
        self, mm: SessionPrincipal, buy: Buy, make_line: MakeLine
    ) -> None:
        line = make_line("10")
        draft = buy((line, "4"), issue=False)
        services.unallocate(mm, allocation(draft, line).pk)
        po_line = only_line(draft)
        assert book_for_order_lines([po_line.pk]).of_order_line(po_line.pk) == []
        (kept,) = book_for_order_lines([po_line.pk], empty=True).of_order_line(po_line.pk)
        assert kept.qty == 0
        totals = book_for_order_lines([po_line.pk]).order_line_totals(po_line.pk)
        assert (totals.ordered, totals.allocated, totals.reserved) == q(4, 0, 0)


class FixedReceipts:
    def __init__(self, line: UUID, allocation: UUID) -> None:
        self.line, self.allocation = line, allocation

    def lines(self, ids: object) -> dict[UUID, LineReceipts]:
        return {self.line: LineReceipts(D(2), posted=True)}

    def attributed(self, ids: object) -> dict[UUID, Decimal]:
        return {self.allocation: D(1)}


class TestReceiptSources:
    def test_sources_add_up(self) -> None:
        line, other = UUID(int=1), UUID(int=2)
        first, second = FixedReceipts(line, other), FixedReceipts(line, other)
        undo = [receipts.register_receipt_source(first), receipts.register_receipt_source(second)]
        receipts.register_receipt_source(first)  # once only
        try:
            assert receipts.line_receipts([line, other]) == {
                line: LineReceipts(D(4), posted=True),
                other: LineReceipts(),
            }
            assert receipts.attributed([other]) == {other: D(2)}
            assert receipts.line_receipts([]) == {} and receipts.attributed([]) == {}
        finally:
            for unregister in undo:
                unregister()
                unregister()  # harmless twice
        assert receipts.line_receipts([line]) == {line: LineReceipts()}


class TestDeactivation:
    def test_an_issued_order_blocks_its_branch_and_where_it_ships(
        self, org: Org, people: People, buy: Buy, make_line: MakeLine
    ) -> None:
        issued = buy((make_line("10"), "10"), ship_to=org.warehouses["B Madrid"])
        buy(issue=False)  # a draft blocks nothing
        expected = [OpenDocument("purchase_order", str(issued.number), issued.pk)]
        assert open_documents([org.a_mad.pk]) == sorted(expected + _requests(org))
        assert open_documents([org.b_mad.pk]) == sorted(expected + _requests(org))
        assert open_documents([org.a_sev.pk]) == []
        admin = principal_for(people.group_admin)
        with pytest.raises(DeactivationRefused):
            organizations.update_branch(admin, org.b_mad.pk, is_active=False)


def _requests(org: Org) -> list[OpenDocument]:
    """The approved hospital requests with outstanding quantity also block (M4.2)."""
    return [
        document
        for document in open_documents([org.a_mad.pk])
        if document.kind == "material_request"
    ]
