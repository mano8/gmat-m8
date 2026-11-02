"""The rules of ``post_receipt`` and ``void_receipt`` (plan §4.5 to §4.9, C7, C10, I4, I7, I8).

The worked examples are in ``test_examples``, who may call what in ``test_matrix``.
"""

from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.access.principals import SessionPrincipal
from apps.audit.models import AuditEvent
from apps.catalog.models import Material
from apps.core import events
from apps.core.idempotency import IdempotencyConflict, issue_key
from apps.handovers.models import Handover
from apps.locations.models import Location
from apps.material_requests import services as request_services
from apps.material_requests.models import MaterialRequest
from apps.organizations.models import Branch
from apps.projects.models import Project
from apps.purchasing import services as purchasing
from apps.purchasing.models import PurchaseOrder
from apps.purchasing.services import LineAmendment
from apps.receiving import services
from apps.receiving.models import GoodsReceipt, GoodsReceiptLine, ReceiptAttribution
from apps.receiving.services import ReceiptLineInput
from tests.org import Org, People
from tests.support import error_codes, principal_for
from tests.unit.purchasing.conftest import Buy, MakeLine, allocation, flows, only_line, q
from tests.unit.receiving.conftest import Receive

pytestmark = pytest.mark.django_db

D = Decimal
Issue = GoodsReceiptLine.Issue


@pytest.fixture
def published() -> Iterator[list[events.Event]]:
    found: list[events.Event] = []
    names = ("receipt.posted", "receipt.voided", "handover.posted", "handover.voided")
    undo = [events.subscribe(name, found.append) for name in names]
    yield found
    for item in undo:
        item()


def refused(exc: Any) -> set[str]:
    return error_codes(exc)


class TestPost:
    def test_a_warehouse_receipt(
        self,
        org: Org,
        people: People,
        make_line: MakeLine,
        buy: Buy,
        receive: Receive,
        published: list[events.Event],
    ) -> None:
        line = make_line("10")
        order = buy((line, "10"))
        receipt = receive(order, "4", delivery_note_ref=" ALB-77 ", notes=" Pallet 1 ")
        year = timezone.now().year
        assert receipt.number == f"REC-A-MAD-{year}-00001"
        assert (receipt.branch, receipt.location) == (org.a_mad, org.warehouses["A Madrid"])
        assert (receipt.received_by, receipt.status) == (people.mm_mad, GoodsReceipt.Status.POSTED)
        assert (receipt.delivery_note_ref, receipt.notes) == ("ALB-77", "Pallet 1")
        (row,) = receipt.lines.all()
        assert (row.po, row.po_line, row.qty_received, row.surplus_qty) == (
            order,
            only_line(order),
            4,
            0,
        )
        (attribution,) = row.attributions.all()
        assert (attribution.allocation, attribution.po_line, attribution.qty) == (
            allocation(order, line),
            only_line(order),
            4,
        )
        assert not Handover.objects.exists()  # a warehouse receipt hands nothing over
        event = AuditEvent.objects.get(action="receipt.posted")
        assert event.data["location_differs"] is False
        assert event.data["lines"][0]["attributions"][0]["qty"] == "4"
        assert [item.name for item in published] == ["receipt.posted"]

    def test_a_site_receipt_hands_over_at_once(
        self,
        org: Org,
        people: People,
        make_line: MakeLine,
        buy: Buy,
        receive: Receive,
        published: list[events.Event],
    ) -> None:
        line = make_line("10")
        order = buy((line, "10"))  # ships to A-MAD's warehouse
        site = org.worksite(org.hospital)
        receipt = receive(order, "4", at=site, principal=principal_for(people.site_manager))
        handover = Handover.objects.get(goods_receipt=receipt)
        year = timezone.now().year
        assert handover.number == f"ENT-A-MAD-{year}-00001"
        assert (handover.kind, handover.branch, handover.project) == (
            Handover.Kind.DIRECT_RECEIPT,
            org.a_mad,
            org.hospital,
        )
        assert (handover.source_location, handover.handed_at) == (site, receipt.received_at)
        assert handover.handed_by == handover.received_by == people.site_manager
        assert AuditEvent.objects.get(action="receipt.posted").data["location_differs"] is True
        assert [item.name for item in published] == ["receipt.posted", "handover.posted"]
        assert (flows(line).received, flows(line).delivered) == q(4, 4)

    def test_site_surplus_alone_hands_nothing_over(
        self, org: Org, make_line: MakeLine, buy: Buy, receive: Receive
    ) -> None:
        line = make_line("10", project=org.sev_for_mad)
        hospital = make_line("10", project=org.hospital)
        order = buy((line, "10"), (hospital, "10"))
        receive(order, "10", at=org.worksite(org.hospital))  # the hospital's 10 handed over
        receipt = receive(order, "5", at=org.worksite(org.hospital))
        assert receipt.lines.get().surplus_qty == 5
        assert not Handover.objects.filter(goods_receipt=receipt).exists()

    def test_rejected_goods_are_recorded_without_counting(
        self, make_line: MakeLine, buy: Buy, receive: Receive, mm: SessionPrincipal
    ) -> None:
        line = make_line("10")
        order = buy((line, "10"))
        item = ReceiptLineInput(
            only_line(order).pk,
            D(0),
            qty_rejected=D(3),
            issue=Issue.DAMAGED,
            issue_note=" Crushed reels ",
        )
        receipt = receive(order, item)
        row = receipt.lines.get()
        assert (row.qty_received, row.qty_rejected, row.issue, row.issue_note) == (
            0,
            3,
            Issue.DAMAGED,
            "Crushed reels",
        )
        assert not row.attributions.exists()
        assert flows(line).received == 0
        # A posted receipt line keeps the order and the line's price as they are (§4.4).
        with pytest.raises(ValidationError) as exc:
            purchasing.cancel(mm, order.pk, reason="Not needed")
        assert refused(exc) == {"has_receipts"}
        with pytest.raises(ValidationError) as exc:
            purchasing.amend(
                mm,
                order.pk,
                reason="New price",
                lines=[LineAmendment(only_line(order).pk, unit_price=D(2))],
            )
        assert refused(exc) == {"price_after_receipt"}

    def test_a_short_delivery_is_an_incident_with_what_arrived(
        self, make_line: MakeLine, buy: Buy, receive: Receive
    ) -> None:
        line = make_line("10")
        order = buy((line, "10"))
        item = ReceiptLineInput(
            only_line(order).pk, D(7), issue=Issue.SHORT, issue_note="3 missing"
        )
        receipt = receive(order, item)
        assert receipt.lines.get().issue == Issue.SHORT
        assert flows(line).received == 7

    def test_several_lines_in_one_receipt(
        self,
        make_line: MakeLine,
        buy: Buy,
        receive: Receive,
        breaker: Material,
    ) -> None:
        cables, breakers = make_line("10"), make_line("4", material=breaker)
        order = buy((cables, "10"), (breakers, "4"))
        first, second = order.lines.order_by("line_no")
        receipt = receive(order, (first, "10"), (second, "4"))
        assert receipt.lines.count() == 2
        assert (flows(cables).received, flows(breakers).received) == q(10, 4)
        assert PurchaseOrder.objects.get(pk=order.pk).status == PurchaseOrder.Status.CLOSED

    def test_a_warehouse_of_a_managed_branch(
        self, org: Org, make_line: MakeLine, buy: Buy, receive: Receive
    ) -> None:
        order = buy((make_line("10"), "10"))
        receipt = receive(order, "3", at=org.warehouses["B Madrid"])
        assert receipt.location == org.warehouses["B Madrid"]

    def test_an_on_hold_project_still_receives_on_site(
        self, org: Org, make_line: MakeLine, buy: Buy, receive: Receive
    ) -> None:
        line = make_line("10")
        order = buy((line, "10"))
        Project.objects.filter(pk=org.hospital.pk).update(status=Project.Status.ON_HOLD)
        receive(order, "3", at=org.worksite(org.hospital))
        assert flows(line).delivered == 3

    def test_idempotent(
        self, people: People, make_line: MakeLine, buy: Buy, receive: Receive
    ) -> None:
        order = buy((make_line("10"), "10"))
        key = issue_key(people.mm_mad.pk, "receipt.post")
        first = receive(order, "3", idempotency_key=key)
        again = receive(order, "3", idempotency_key=key)
        assert again == first
        assert GoodsReceipt.objects.count() == 1
        with pytest.raises(IdempotencyConflict):
            receive(order, "4", idempotency_key=key)

    def test_the_payload_binds_a_manual_split(
        self, people: People, make_line: MakeLine, buy: Buy, receive: Receive
    ) -> None:
        line = make_line("10")
        order = buy((line, "10"))
        key = issue_key(people.mm_mad.pk, "receipt.post")
        split = {allocation(order, line).pk: D(2)}
        item = ReceiptLineInput(only_line(order).pk, D(3), split=split)
        first = receive(order, item, idempotency_key=key)
        assert first.lines.get().surplus_qty == 1
        with pytest.raises(IdempotencyConflict):
            receive(
                order,
                ReceiptLineInput(
                    only_line(order).pk, D(3), split={allocation(order, line).pk: D(3)}
                ),
                idempotency_key=key,
            )


class TestManualSplit:
    def test_a_split_within_the_caps(self, make_line: MakeLine, buy: Buy, receive: Receive) -> None:
        x, y = make_line("10"), make_line("10")
        order = buy((x, "10"), (y, "10"))
        split = {allocation(order, y).pk: D(6), allocation(order, x).pk: D(0)}
        receipt = receive(order, ReceiptLineInput(only_line(order).pk, D(8), split=split))
        assert {a.allocation.request_line for a in receipt.lines.get().attributions.all()} == {y}
        assert receipt.lines.get().surplus_qty == 2
        assert (flows(x).received, flows(y).received) == q(0, 6)
        # The waterfall serves X first again: 12 remain, X 10 and Y the 2 left of its 4.
        assert (flows(x).pending, flows(y).pending) == q(10, 2)

    def test_more_than_accepted_is_refused(
        self, make_line: MakeLine, buy: Buy, receive: Receive
    ) -> None:
        x, y = make_line("10"), make_line("10")
        order = buy((x, "10"), (y, "10"))
        split = {allocation(order, x).pk: D(3), allocation(order, y).pk: D(3)}
        with pytest.raises(ValidationError) as exc:
            receive(order, ReceiptLineInput(only_line(order).pk, D(5), split=split))
        assert refused(exc) == {"exceeds_accepted"}

    def test_an_allocation_of_another_project_is_refused_on_site(
        self, org: Org, make_line: MakeLine, buy: Buy, receive: Receive
    ) -> None:
        x = make_line("10", project=org.hospital)
        y = make_line("10", project=org.sev_for_mad)
        order = buy((x, "10"), (y, "10"))
        split = {allocation(order, y).pk: D(2)}
        with pytest.raises(ValidationError) as exc:
            receive(
                order,
                ReceiptLineInput(only_line(order).pk, D(2), split=split),
                at=org.worksite(org.hospital),
            )
        assert refused(exc) == {"not_candidate"}

    def test_a_part_in_the_wrong_precision_is_refused(
        self, make_line: MakeLine, buy: Buy, receive: Receive, breaker: Material
    ) -> None:
        line = make_line("4", material=breaker)
        order = buy((line, "4"))
        split = {allocation(order, line).pk: D("1.5")}
        with pytest.raises(ValidationError) as exc:
            receive(order, ReceiptLineInput(only_line(order).pk, D(2), split=split))
        assert refused(exc) == {"precision"}


class TestCandidates:
    def test_a_closed_request_takes_nothing(
        self, make_line: MakeLine, buy: Buy, receive: Receive, mm: SessionPrincipal
    ) -> None:
        x, y = make_line("10"), make_line("10")
        order = buy((x, "10"), (y, "10"))
        request_services.manual_close(mm, x.request_id, reason="Not needed after all")
        receipt = receive(order, "10")
        rows = receipt.lines.get().attributions.all()
        assert {row.allocation.request_line for row in rows} == {y}

    def test_an_allocation_received_in_full_takes_nothing(
        self, make_line: MakeLine, buy: Buy, receive: Receive
    ) -> None:
        x, y = make_line("5"), make_line("10")
        order = buy((x, "5"), (y, "10"))
        receive(order, "5")
        receipt = receive(order, "4")
        rows = receipt.lines.get().attributions.all()
        assert [(row.allocation.request_line, row.qty) for row in rows] == [(y, 4)]

    def test_nothing_open_leaves_everything_surplus(
        self, make_line: MakeLine, buy: Buy, receive: Receive, mm: SessionPrincipal
    ) -> None:
        line = make_line("10")
        order = buy((line, "10"))
        request_services.manual_close(mm, line.request_id, reason="Project cancelled")
        receipt = receive(order, "3")
        assert receipt.lines.get().surplus_qty == 3
        assert not ReceiptAttribution.objects.exists()


class TestRefusals:
    def test_no_lines(self, make_line: MakeLine, buy: Buy, receive: Receive) -> None:
        order = buy((make_line("10"), "10"))
        with pytest.raises(ValidationError) as exc:
            receive(order)
        assert refused(exc) == {"empty"}

    def test_one_order_line_twice(self, make_line: MakeLine, buy: Buy, receive: Receive) -> None:
        order = buy((make_line("10"), "10"))
        with pytest.raises(ValueError, match="once per receipt"):
            receive(order, "1", "2")

    def test_a_line_of_another_order(self, make_line: MakeLine, buy: Buy, receive: Receive) -> None:
        line = make_line("10")
        order, other = buy((line, "5")), buy((line, "5"))
        with pytest.raises(ValidationError) as exc:
            receive(order, (only_line(other), "1"))
        assert refused(exc) == {"unknown"}

    @pytest.mark.parametrize(
        ("received", "rejected", "code"),
        [("-1", "0", "positive"), ("1.234", "0", "precision"), ("0", "0", "empty_line")],
    )
    def test_quantities(
        self,
        make_line: MakeLine,
        buy: Buy,
        receive: Receive,
        received: str,
        rejected: str,
        code: str,
    ) -> None:
        order = buy((make_line("10"), "10"))
        item = ReceiptLineInput(only_line(order).pk, D(received), qty_rejected=D(rejected))
        with pytest.raises(ValidationError) as exc:
            receive(order, item)
        assert refused(exc) == {code}

    def test_an_unknown_incident(self, make_line: MakeLine, buy: Buy, receive: Receive) -> None:
        order = buy((make_line("10"), "10"))
        with pytest.raises(ValidationError) as exc:
            receive(order, ReceiptLineInput(only_line(order).pk, D(1), issue="LOST"))
        assert refused(exc) == {"invalid"}

    def test_over_receipt(self, make_line: MakeLine, buy: Buy, receive: Receive) -> None:
        order = buy((make_line("10"), "10"))
        receive(order, "8")
        with pytest.raises(ValidationError) as exc:
            receive(order, "3")
        assert refused(exc) == {"over_receipt"}

    def test_a_line_closed_short(
        self,
        make_line: MakeLine,
        buy: Buy,
        receive: Receive,
        mm: SessionPrincipal,
        breaker: Material,
    ) -> None:
        cables, breakers = make_line("10"), make_line("4", material=breaker)
        order = buy((cables, "10"), (breakers, "4"))
        first, _second = order.lines.order_by("line_no")
        purchasing.close_line_short(mm, first.pk, reason="Discontinued")
        with pytest.raises(ValidationError) as exc:
            receive(order, (first, "1"))
        assert refused(exc) == {"no_capacity"}

    @pytest.mark.parametrize("status", ["DRAFT", "CLOSED"])
    def test_an_order_that_is_not_issued(
        self, make_line: MakeLine, buy: Buy, receive: Receive, status: str
    ) -> None:
        order = buy((make_line("10"), "10"), issue=status != "DRAFT")
        if status == "CLOSED":
            receive(order, "10")
        with pytest.raises(ValidationError) as exc:
            receive(order, "1")
        assert refused(exc) == {"not_receivable"}

    def test_an_inactive_branch(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy, receive: Receive
    ) -> None:
        order = buy((make_line("10"), "10"))
        Branch.objects.filter(pk=org.a_mad.pk).update(is_active=False)
        with pytest.raises(ValidationError) as exc:
            receive(order, "1", principal=principal_for(people.group_admin))
        assert refused(exc) == {"inactive_branch"}


class TestLocation:
    """C7 and S9: where goods of an order may arrive."""

    def _refused(self, receive: Receive, order: PurchaseOrder, at: Location) -> None:
        with pytest.raises(ValidationError) as exc:
            receive(order, "1", at=at)
        assert refused(exc) == {"location"}

    def test_a_warehouse_of_another_branch(
        self, org: Org, make_line: MakeLine, buy: Buy, receive: Receive
    ) -> None:
        self._refused(receive, buy((make_line("10"), "10")), org.warehouses["A Sevilla"])

    def test_the_worksite_of_a_project_the_order_does_not_serve(
        self, org: Org, make_line: MakeLine, buy: Buy, receive: Receive
    ) -> None:
        order = buy((make_line("10"), "10"))
        self._refused(receive, order, org.worksite(org.sev_for_mad))

    def test_the_worksite_of_a_closed_project(
        self, org: Org, make_line: MakeLine, buy: Buy, receive: Receive
    ) -> None:
        order = buy((make_line("10"), "10"))
        Project.objects.filter(pk=org.hospital.pk).update(status=Project.Status.CLOSED)
        self._refused(receive, order, org.worksite(org.hospital))
        receive(order, "1")  # a warehouse still takes them (S9)

    def test_an_inactive_location(
        self, org: Org, make_line: MakeLine, buy: Buy, receive: Receive
    ) -> None:
        order = buy((make_line("10"), "10"))
        Location.objects.filter(pk=org.warehouses["A Madrid"].pk).update(is_active=False)
        self._refused(receive, order, org.warehouses["A Madrid"])

    def test_a_location_of_an_inactive_branch(
        self, org: Org, make_line: MakeLine, buy: Buy, receive: Receive
    ) -> None:
        order = buy((make_line("10"), "10"))
        Branch.objects.filter(pk=org.b_mad.pk).update(is_active=False)
        self._refused(receive, order, org.warehouses["B Madrid"])


class TestVoid:
    def test_void(
        self,
        people: People,
        make_line: MakeLine,
        buy: Buy,
        receive: Receive,
        mm: SessionPrincipal,
        published: list[events.Event],
    ) -> None:
        line = make_line("10")
        order = buy((line, "10"))
        receipt = receive(order, "4")
        voided = services.void_receipt(mm, receipt.pk, reason="  Wrong order  ")
        assert voided.status == GoodsReceipt.Status.VOIDED
        assert (voided.voided_by, voided.void_reason) == (people.mm_mad, "Wrong order")
        assert voided.voided_at is not None
        assert (flows(line).received, flows(line).pending) == q(0, 10)
        event = AuditEvent.objects.get(action="receipt.voided")
        assert (event.data["reason"], event.data["from_status"]) == ("Wrong order", "POSTED")
        assert [item.name for item in published] == ["receipt.posted", "receipt.voided"]

    def test_a_site_receipt_voids_its_handover(
        self,
        org: Org,
        make_line: MakeLine,
        buy: Buy,
        receive: Receive,
        mm: SessionPrincipal,
        published: list[events.Event],
    ) -> None:
        line = make_line("10")
        order = buy((line, "10"))
        receipt = receive(order, "4", at=org.worksite(org.hospital))
        services.void_receipt(mm, receipt.pk, reason="Counted twice")
        handover = Handover.objects.get(goods_receipt=receipt)
        assert (handover.status, handover.void_reason) == (Handover.Status.VOIDED, "Counted twice")
        assert handover.voided_at == GoodsReceipt.objects.get(pk=receipt.pk).voided_at
        assert (flows(line).received, flows(line).delivered) == q(0, 0)
        assert [item.name for item in published][-2:] == ["receipt.voided", "handover.voided"]

    def test_a_reason_is_required(
        self, make_line: MakeLine, buy: Buy, receive: Receive, mm: SessionPrincipal
    ) -> None:
        receipt = receive(buy((make_line("10"), "10")), "4")
        with pytest.raises(ValidationError) as exc:
            services.void_receipt(mm, receipt.pk, reason="  ")
        assert refused(exc) == {"required"}

    def test_twice(
        self, make_line: MakeLine, buy: Buy, receive: Receive, mm: SessionPrincipal
    ) -> None:
        receipt = receive(buy((make_line("10"), "10")), "4")
        services.void_receipt(mm, receipt.pk, reason="Wrong order")
        with pytest.raises(ValidationError) as exc:
            services.void_receipt(mm, receipt.pk, reason="Again")
        assert refused(exc) == {"transition"}

    def test_re_entering_after_a_void(
        self, make_line: MakeLine, buy: Buy, receive: Receive, mm: SessionPrincipal
    ) -> None:
        line = make_line("10")
        order = buy((line, "10"))
        receipt = receive(order, "10")
        assert PurchaseOrder.objects.get(pk=order.pk).status == PurchaseOrder.Status.CLOSED
        services.void_receipt(mm, receipt.pk, reason="Wrong quantity")
        receive(order, "9")
        assert flows(line).received == 9
        assert PurchaseOrder.objects.get(pk=order.pk).status == PurchaseOrder.Status.ISSUED


class TestRequestsWithReceivedGoods:
    """A request with received goods is neither cancelled nor returned to draft (I13)."""

    def _received_in_full(self, make_line: MakeLine, buy: Buy, receive: Receive) -> MaterialRequest:
        line = make_line("10")
        order = buy((line, "10"))
        receive(order, "10")  # at a warehouse: the order closes, nothing is allocated
        assert flows(line).allocated == 0
        return MaterialRequest.objects.get(pk=line.request_id)

    def test_cancel(
        self, make_line: MakeLine, buy: Buy, receive: Receive, mm: SessionPrincipal
    ) -> None:
        request = self._received_in_full(make_line, buy, receive)
        with pytest.raises(ValidationError) as exc:
            request_services.cancel(mm, request.pk, reason="Not needed")
        assert refused(exc) == {"in_progress"}

    def test_return_to_draft(
        self, make_line: MakeLine, buy: Buy, receive: Receive, mm: SessionPrincipal
    ) -> None:
        request = self._received_in_full(make_line, buy, receive)
        with pytest.raises(ValidationError) as exc:
            request_services.return_to_draft(mm, request.pk, note="Start again")
        assert refused(exc) == {"in_progress"}
