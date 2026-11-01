"""The M6.1 done-when list: receipts, attribution and voids against plan §4.5's examples.

Every example runs through the services, with real receipts and the direct handovers they
create at a worksite:

- 100 ordered → 60 + 30 received → 10 outstanding;
- E2, E3, E4, E6, E7, E8, E12 and E13 (with a manual split that tries to exceed the caps);
- a site receipt is never counted twice;
- voiding a receipt reopens an auto-closed request and a closed purchase order.

Concurrent receipts and the C1-C2 keys are in ``tests/integration``.
"""

from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from apps.access.principals import SessionPrincipal
from apps.audit.models import AuditEvent
from apps.catalog.models import Material
from apps.handovers.models import Handover
from apps.material_requests import services as request_services
from apps.material_requests.models import MaterialRequest, MaterialRequestLine
from apps.projects.models import Project
from apps.purchasing import services as purchasing
from apps.purchasing.ledger import book_for_order_lines
from apps.purchasing.models import PurchaseOrder
from apps.receiving import services
from apps.receiving.models import GoodsReceipt
from apps.receiving.services import ReceiptLineInput
from tests.org import Org
from tests.support import error_codes
from tests.unit.purchasing.conftest import (
    Buy,
    MakeLine,
    allocation,
    flows,
    ledger_of,
    only_line,
    q,
)
from tests.unit.receiving.conftest import Receive

pytestmark = pytest.mark.django_db

D = Decimal
Status = PurchaseOrder.Status


def attributed(receipt: GoodsReceipt) -> dict[MaterialRequestLine, Decimal]:
    rows = (
        receipt.lines.get()
        .attributions.select_related("allocation__request_line")
        .order_by("allocation__request_line__request__submitted_at")
    )
    return {row.allocation.request_line: row.qty for row in rows}


def surplus(receipt: GoodsReceipt) -> Decimal:
    return Decimal(receipt.lines.get().surplus_qty)


def line_totals(order: PurchaseOrder) -> tuple[Decimal, ...]:
    line = only_line(order)
    totals = book_for_order_lines([line.pk]).order_line_totals(line.pk)
    return (totals.received, totals.remaining, totals.pending, totals.unallocated)


def status_of(item: PurchaseOrder | MaterialRequestLine) -> str:
    if isinstance(item, PurchaseOrder):
        return PurchaseOrder.objects.get(pk=item.pk).status
    return MaterialRequest.objects.get(pk=item.request_id).status


def trims(reason: str) -> list[tuple[Decimal, Decimal]]:
    events = AuditEvent.objects.filter(action="purchase_allocation.trimmed").order_by("id")
    return [
        (D(event.data["from"]), D(event.data["to"]))
        for event in events
        if event.data["reason"] == reason
    ]


class TestOutstanding:
    def test_100_ordered_60_and_30_received_leave_10_outstanding(
        self, make_line: MakeLine, buy: Buy, receive: Receive
    ) -> None:
        line = make_line("100")
        order = buy((line, "100"))
        receive(order, "60")
        receive(order, "30")
        totals = book_for_order_lines([only_line(order).pk]).order_line_totals(only_line(order).pk)
        assert (totals.received, totals.remaining, totals.outstanding) == q(90, 10, 10)
        found = flows(line)
        assert (found.received, found.pending, found.delivered) == q(90, 10, 0)
        assert ledger_of(line).awaiting_handover == 90
        assert status_of(order) == Status.ISSUED

    def test_the_rest_closes_the_order(
        self, make_line: MakeLine, buy: Buy, receive: Receive
    ) -> None:
        line = make_line("100")
        order = buy((line, "100"))
        receive(order, "60")
        receive(order, "40")
        assert status_of(order) == Status.CLOSED
        assert AuditEvent.objects.filter(action="purchase_order.auto_closed").count() == 1
        # Received at a warehouse, nothing is delivered: the request stays open.
        assert status_of(line) == MaterialRequest.Status.APPROVED
        assert ledger_of(line).awaiting_handover == 100


class TestE2:
    def test_voiding_after_a_short_close_revives_nothing(
        self, make_line: MakeLine, buy: Buy, receive: Receive, mm: SessionPrincipal
    ) -> None:
        line = make_line("10")
        order = buy((line, "10"))
        receipt = receive(order, "6")
        purchasing.close_line_short(mm, only_line(order).pk, reason="Supplier ran out")
        assert allocation(order, line).qty == 6  # E1
        assert status_of(order) == Status.CLOSED
        services.void_receipt(mm, receipt.pk, reason="Wrong delivery note")
        found = flows(line)
        assert (found.received, found.pending) == q(0, 0)
        assert ledger_of(line).to_purchase == 10
        # The line is still closed short, so the order has no capacity again.
        assert status_of(order) == Status.CLOSED


class TestE3AndE4:
    def _e3(
        self, org: Org, make_line: MakeLine, buy: Buy, receive: Receive
    ) -> tuple[MaterialRequestLine, MaterialRequestLine, PurchaseOrder, GoodsReceipt]:
        x = make_line("10", project=org.hospital)
        y = make_line("10", project=org.sev_for_mad)
        order = buy((x, "10"), (y, "10"))
        receipt = receive(order, "20", at=org.worksite(org.hospital))
        return x, y, order, receipt

    def test_e3_everything_at_one_site_leaves_site_surplus(
        self, org: Org, make_line: MakeLine, buy: Buy, receive: Receive
    ) -> None:
        x, y, order, receipt = self._e3(org, make_line, buy, receive)
        assert attributed(receipt) == {x: 10}
        assert surplus(receipt) == 10
        assert flows(x).received == 10
        # The line has no capacity left: Y waits for nothing and must be bought again.
        assert flows(y).pending == 0
        assert ledger_of(y).to_purchase == 10
        assert status_of(order) == Status.CLOSED
        # X was handed over on site in full: its request closed automatically.
        assert flows(x).delivered == 10
        assert status_of(x) == MaterialRequest.Status.CLOSED

    def test_e4_voiding_it_reopens_the_order_and_revives_both(
        self, org: Org, make_line: MakeLine, buy: Buy, receive: Receive, mm: SessionPrincipal
    ) -> None:
        x, y, order, receipt = self._e3(org, make_line, buy, receive)
        services.void_receipt(mm, receipt.pk, reason="Entered on the wrong order")
        assert status_of(order) == Status.ISSUED
        assert status_of(x) == MaterialRequest.Status.APPROVED
        assert (flows(x).pending, flows(y).pending) == q(10, 10)
        assert (flows(x).received, flows(x).delivered) == q(0, 0)
        assert Handover.objects.get(goods_receipt=receipt).status == Handover.Status.VOIDED
        assert trims("EXCESS_COMMITMENT") == []


class TestE6:
    def test_a_warehouse_receipt_serves_the_attribution_order(
        self, make_line: MakeLine, buy: Buy, receive: Receive
    ) -> None:
        x, y, z = make_line("20"), make_line("15"), make_line("10")
        order = buy((x, "20"), (y, "15"), (z, "10"))
        receipt = receive(order, "30")
        assert attributed(receipt) == {x: 20, y: 10}
        assert surplus(receipt) == 0
        assert [flows(item).received for item in (x, y, z)] == [20, 10, 0]
        assert [flows(item).pending for item in (x, y, z)] == [0, 5, 10]

    def test_priority_comes_first(self, make_line: MakeLine, buy: Buy, receive: Receive) -> None:
        x, y = make_line("20"), make_line("15", priority=MaterialRequest.Priority.URGENT)
        order = buy((x, "20"), (y, "15"))
        receipt = receive(order, "30")
        assert attributed(receipt) == {y: 15, x: 15}


class TestE7:
    def test_site_surplus_never_makes_unallocated_negative(
        self, org: Org, make_line: MakeLine, buy: Buy, receive: Receive
    ) -> None:
        x = make_line("10", project=org.hospital)
        y = make_line("10", project=org.sev_for_mad)
        order = buy((x, "10"), (y, "10"))
        receipt = receive(order, "15", at=org.worksite(org.hospital))
        assert attributed(receipt) == {x: 10}
        assert surplus(receipt) == 5
        assert (flows(x).pending, flows(y).pending) == q(0, 5)
        assert ledger_of(y).to_purchase == 5
        assert line_totals(order) == q(15, 5, 5, 0)


class TestE8:
    def test_a_void_after_a_replacement_purchase_trims_the_revived_pending(
        self,
        org: Org,
        make_line: MakeLine,
        buy: Buy,
        receive: Receive,
        mm: SessionPrincipal,
    ) -> None:
        x = make_line("10", project=org.hospital)
        y = make_line("10", project=org.sev_for_mad)
        first = buy((x, "10"), (y, "10"))
        receipt = receive(first, "20", at=org.worksite(org.hospital))  # E3
        second = buy((y, "10"))  # Y's missing 10, bought again
        services.void_receipt(mm, receipt.pk, reason="Entered on the wrong order")
        assert status_of(first) == Status.ISSUED
        assert allocation(first, y).qty == 0
        assert trims("EXCESS_COMMITMENT") == [q(10, 0)]
        assert (flows(y).pending, ledger_of(y).committed) == q(10, 10)
        assert allocation(second, y).qty == 10
        assert flows(x).pending == 10
        assert line_totals(first)[3] == 10  # unallocated on order, flagged


class TestE12:
    def test_goods_at_a_later_allocations_site_are_attributed_to_it(
        self,
        org: Org,
        third: Project,
        make_line: MakeLine,
        buy: Buy,
        receive: Receive,
        breaker: Material,
    ) -> None:
        x = make_line("10", project=org.hospital)
        y = make_line("10", project=org.sev_for_mad)
        w = make_line("5", project=third, material=breaker)  # makes W's site valid (C7)
        order = buy((x, "10"), (y, "10"), (w, "5"))
        cable_line = order.lines.get(material=x.material)
        unrelated = receive(order, (cable_line, "15"), at=org.worksite(third))
        assert surplus(unrelated) == 15
        assert (flows(x).pending, flows(y).pending) == q(5, 0)
        at_y = receive(order, (cable_line, "5"), at=org.worksite(org.sev_for_mad))
        assert attributed(at_y) == {y: 5}
        assert surplus(at_y) == 0
        assert (flows(y).received, flows(y).delivered) == q(5, 5)
        assert flows(x).pending == 0


class TestE13:
    def _setup(
        self, org: Org, third: Project, make_line: MakeLine, buy: Buy, receive: Receive
    ) -> tuple[MaterialRequestLine, PurchaseOrder]:
        x = make_line("10", project=org.hospital)
        y = make_line("10", project=org.sev_for_mad)
        z = make_line("10", project=third)
        first = buy((x, "10"), (y, "10"), (z, "10"))
        receive(first, "15", at=org.worksite(org.hospital))  # Y 10 / Z 5 pending
        assert (flows(y).pending, flows(z).pending) == q(10, 5)
        second = buy((z, "5"))  # Z's missing 5 on a replacement order
        receive(second, "5")  # at a warehouse: R = 5
        return z, first

    def test_the_receipt_capacity_caps_a_site_attribution(
        self, org: Org, third: Project, make_line: MakeLine, buy: Buy, receive: Receive
    ) -> None:
        z, first = self._setup(org, third, make_line, buy, receive)
        receipt = receive(first, "6", at=org.worksite(third))
        assert attributed(receipt) == {z: 5}
        assert surplus(receipt) == 1
        found = flows(z)
        assert (found.received, found.delivered, found.pending) == q(10, 5, 0)
        assert ledger_of(z).awaiting_handover == 5
        assert ledger_of(z).committed == 10

    def test_a_manual_split_cannot_exceed_the_caps(
        self, org: Org, third: Project, make_line: MakeLine, buy: Buy, receive: Receive
    ) -> None:
        z, first = self._setup(org, third, make_line, buy, receive)
        z_first = allocation(first, z)
        line = only_line(first)
        with pytest.raises(ValidationError) as exc:
            receive(
                first,
                ReceiptLineInput(line.pk, D(6), split={z_first.pk: D(6)}),
                at=org.worksite(third),
            )
        assert error_codes(exc) == {"exceeds_capacity"}
        receipt = receive(
            first,
            ReceiptLineInput(line.pk, D(6), split={z_first.pk: D(5)}),
            at=org.worksite(third),
        )
        assert attributed(receipt) == {z: 5}
        assert surplus(receipt) == 1
        assert flows(z).received == 10  # never 11


class TestNeverCountedTwice:
    def test_a_site_receipt_is_received_and_delivered_once(
        self, org: Org, make_line: MakeLine, buy: Buy, receive: Receive
    ) -> None:
        line = make_line("10")
        order = buy((line, "10"), ship_to=org.worksite(org.hospital))
        receipt = receive(order, "10", at=org.worksite(org.hospital))
        found = flows(line)
        assert (found.received, found.delivered) == q(10, 10)
        assert ledger_of(line).committed == 0  # closed: delivered in full
        handover = Handover.objects.get(goods_receipt=receipt)
        assert handover.kind == Handover.Kind.DIRECT_RECEIPT
        (handed,) = handover.lines.all()
        (attribution,) = receipt.lines.get().attributions.all()
        assert (handed.receipt_attribution, handed.qty) == (attribution, attribution.qty)
        assert handed.request_line == line

    def test_two_site_receipts_add_up_once(
        self, org: Org, make_line: MakeLine, buy: Buy, receive: Receive
    ) -> None:
        line = make_line("10")
        order = buy((line, "10"))
        receive(order, "7", at=org.worksite(org.hospital))
        assert (flows(line).received, flows(line).delivered) == q(7, 7)
        assert ledger_of(line).awaiting_handover == 0
        second = receive(order, "3", at=org.worksite(org.hospital))
        assert attributed(second) == {line: 3}
        assert (flows(line).received, flows(line).delivered) == q(10, 10)
        assert status_of(line) == MaterialRequest.Status.CLOSED


class TestVoidReopens:
    def test_voiding_reopens_an_auto_closed_request_and_a_closed_order(
        self, org: Org, make_line: MakeLine, buy: Buy, receive: Receive, mm: SessionPrincipal
    ) -> None:
        line = make_line("10")
        order = buy((line, "10"))
        receipt = receive(order, "10", at=org.worksite(org.hospital))
        assert (status_of(order), status_of(line)) == (Status.CLOSED, "CLOSED")
        services.void_receipt(mm, receipt.pk, reason="Counted twice by mistake")
        assert (status_of(order), status_of(line)) == (Status.ISSUED, "APPROVED")
        assert flows(line).pending == 10
        actions = set(AuditEvent.objects.values_list("action", flat=True))
        assert {"purchase_order.auto_reopened", "request.auto_reopened"} <= actions

    def test_a_manually_closed_request_stays_closed(
        self, org: Org, make_line: MakeLine, buy: Buy, receive: Receive, mm: SessionPrincipal
    ) -> None:
        line = make_line("10")
        order = buy((line, "10"))
        receipt = receive(order, "4", at=org.worksite(org.hospital))
        request_services.manual_close(mm, line.request_id, reason="Project scaled down")
        services.void_receipt(mm, receipt.pk, reason="Wrong project")
        assert status_of(line) == MaterialRequest.Status.CLOSED
        assert status_of(order) == Status.ISSUED
