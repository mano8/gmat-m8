"""The M5.1 done-when list: the worked examples of plan §4.5 and the cases around them.

Every example runs through the services. Receipts (M6.1) and handovers (M6.2) do not exist
yet: ``received`` posts what a receipt would attribute, ``delivered`` what a direct
receipt at a worksite hands over (D).

- 20 + 15 + 10 = 45: three request lines of one material make one order line;
- one request line across two orders;
- E1, E5, E9, E10 (both variants), E11 and a cascade where a cut hands quantity to a third
  request that is then cut too;
- issue revalidation refuses a draft over-committed by a direct database edit;
- a component above the bound is refused cleanly;
- issued orders keep their snapshots after master-data edits.
"""

from decimal import Decimal
from uuid import UUID

import pytest
from django.core.exceptions import ValidationError

from apps.access.principals import SessionPrincipal
from apps.audit.models import AuditEvent
from apps.catalog.models import Material
from apps.core.models import DocumentSequence
from apps.locations.models import Location
from apps.material_requests import services as requests
from apps.material_requests.models import MaterialRequest, MaterialRequestLine
from apps.organizations.models import Company
from apps.purchasing import components, services
from apps.purchasing.ledger import book_for_order_lines
from apps.purchasing.models import PurchaseAllocation, PurchaseOrder, PurchaseOrderLine
from apps.purchasing.services import RequestItem
from apps.suppliers.models import Supplier
from tests.org import Org, People
from tests.support import error_codes
from tests.unit.purchasing.conftest import (
    Buy,
    FakeReceipts,
    MakeLine,
    allocation,
    flows,
    ledger_of,
    only_line,
    q,
)

pytestmark = pytest.mark.django_db

D = Decimal
Status = PurchaseOrder.Status


def trims(reason: str) -> list[tuple[Decimal, Decimal]]:
    events = AuditEvent.objects.filter(action="purchase_allocation.trimmed").order_by("id")
    return [
        (D(event.data["from"]), D(event.data["to"]))
        for event in events
        if event.data["reason"] == reason
    ]


def second_line(line: MaterialRequestLine) -> MaterialRequestLine:
    """Another live line on ``line``'s request (stock), so closing ``line`` short does not
    close the request automatically."""
    return MaterialRequestLine.objects.create(
        request=line.request,
        project=line.project,
        line_no=2,
        material=line.material,
        unit_id=line.unit_id,
        requested_qty=D(1),
        approved_qty=D(1),
        decision="APPROVED",
        decided_by=line.decided_by,
        decided_at=line.decided_at,
        sourcing="STOCK",
    )


def unallocated(order: PurchaseOrder) -> Decimal:
    line = only_line(order)
    return book_for_order_lines([line.pk]).order_line_totals(line.pk).unallocated


class TestGrouping:
    def test_three_lines_of_one_material_make_one_line_of_45(
        self, make_line: MakeLine, buy: Buy
    ) -> None:
        x, y, z = make_line("20"), make_line("15"), make_line("10")
        order = buy((x, "20"), (y, "15"), (z, "10"), issue=False)
        line = only_line(order)
        assert line.ordered_qty == 45
        assert sorted(a.qty for a in line.allocations.all()) == [10, 15, 20]
        # A draft reserves the demand: nothing is left to purchase.
        assert [flows(item).reserved for item in (x, y, z)] == [20, 15, 10]
        assert {ledger_of(item).to_purchase for item in (x, y, z)} == {0}

    def test_issuing_turns_the_reservations_into_pending(
        self, make_line: MakeLine, buy: Buy
    ) -> None:
        x, y, z = make_line("20"), make_line("15"), make_line("10")
        buy((x, "20"), (y, "15"), (z, "10"))
        assert [(flows(item).reserved, flows(item).pending) for item in (x, y, z)] == [
            q(0, 20),
            q(0, 15),
            q(0, 10),
        ]


class TestSplit:
    def test_one_request_line_across_two_orders(self, make_line: MakeLine, buy: Buy) -> None:
        line = make_line("10")
        first, second = buy((line, "6")), buy((line, "4"))
        found = flows(line)
        assert (found.pending, found.reserved, found.allocated) == q(10, 0, 10)
        assert ledger_of(line).to_purchase == 0
        assert [allocation(order, line).qty for order in (first, second)] == [6, 4]
        with pytest.raises(ValidationError) as exc:
            buy((line, "1"))
        assert error_codes(exc) == {"exceeds_to_purchase"}


class TestE1:
    def test_closing_the_order_line_short_trims_to_what_was_received(
        self,
        make_line: MakeLine,
        buy: Buy,
        received: FakeReceipts,
        mm: SessionPrincipal,
    ) -> None:
        line = make_line("10")
        order = buy((line, "10"))
        received.receive(only_line(order), "6", {allocation(order, line): "6"})
        services.close_line_short(mm, only_line(order).pk, reason="Supplier ran out")
        assert allocation(order, line).qty == 6
        found = flows(line)
        assert (found.received, found.pending) == q(6, 0)
        assert ledger_of(line).to_purchase == 4
        assert trims("ORDER_LINE_CLOSED") == [q(10, 6)]
        # No line has capacity left: the order closes (§4.9).
        assert PurchaseOrder.objects.get(pk=order.pk).status == Status.CLOSED


class TestE5:
    def test_a_reopened_line_keeps_its_trimmed_allocation(
        self,
        make_line: MakeLine,
        buy: Buy,
        received: FakeReceipts,
        mm: SessionPrincipal,
    ) -> None:
        line = make_line("10")
        second_line(line)  # keeps the request open once the first line is closed
        order = buy((line, "10"))
        received.receive(only_line(order), "6", {allocation(order, line): "6"})
        requests.close_line_short(mm, line.pk, reason="Enough for now")
        assert allocation(order, line).qty == 6
        assert trims("REQUEST_LINE_CLOSED") == [q(10, 6)]
        requests.reopen_line(mm, line.pk, reason="Needed after all")
        found = flows(line)
        assert (found.received, found.pending) == q(6, 0)
        assert ledger_of(line).to_purchase == 4
        # The rest of the order line is on order for nobody: flagged.
        assert unallocated(order) == 4


class TestE9:
    def test_two_drafts_compete_for_the_same_quantity(
        self, make_line: MakeLine, buy: Buy, mm: SessionPrincipal
    ) -> None:
        line = make_line("10")
        draft_a, draft_b = buy((line, "10"), issue=False), buy(issue=False)
        with pytest.raises(ValidationError) as exc:
            services.add_request_lines(mm, draft_b.pk, [RequestItem(line.pk, D(10))])
        assert error_codes(exc) == {"exceeds_to_purchase"}
        with pytest.raises(ValidationError) as exc:
            services.add_request_lines(mm, draft_b.pk, [RequestItem(line.pk)])
        assert error_codes(exc) == {"nothing_to_purchase"}
        assert flows(line).reserved == 10
        services.issue(mm, draft_a.pk)
        found = flows(line)
        assert (found.reserved, found.pending) == q(0, 10)


@pytest.fixture
def e10(
    make_line: MakeLine,
    buy: Buy,
    received: FakeReceipts,
    delivered: dict[UUID, Decimal],
) -> tuple[MaterialRequestLine, MaterialRequestLine, MaterialRequestLine, PurchaseOrder]:
    """Order line 30: X 10 / Y 10 / Z 10; 15 received at X's site (X attributed and handed
    over 10, 5 site surplus): Y 10 / Z 5 pending; Z's missing 5 bought on a second order."""
    x, y, z = make_line("10"), make_line("10"), make_line("10")
    first = buy((x, "10"), (y, "10"), (z, "10"))
    received.receive(only_line(first), "15", {allocation(first, x): "10"})
    delivered[x.pk] = D(10)
    assert [flows(item).pending for item in (x, y, z)] == [0, 10, 5]
    buy((z, "5"))
    assert [ledger_of(item).committed for item in (x, y, z)] == [10, 10, 10]
    return x, y, z, first


class TestE10:
    def test_closing_y_short_cuts_z_on_the_first_order(
        self,
        e10: tuple[MaterialRequestLine, MaterialRequestLine, MaterialRequestLine, PurchaseOrder],
        mm: SessionPrincipal,
    ) -> None:
        _x, y, z, first = e10
        requests.close_line_short(mm, y.pk, reason="Not needed")
        # The waterfall would give Z 10 on the first order (committed 15): cut to 5.
        assert allocation(first, z).qty == 5
        assert flows(z).pending == 10  # 5 on each order
        assert ledger_of(z).committed == 10
        assert trims("EXCESS_COMMITMENT") == [q(10, 5)]
        assert unallocated(first) == 10  # flagged for the material manager

    def test_moving_z_ahead_of_y_cuts_z_on_the_first_order(
        self,
        e10: tuple[MaterialRequestLine, MaterialRequestLine, MaterialRequestLine, PurchaseOrder],
        mm: SessionPrincipal,
    ) -> None:
        _x, y, z, first = e10
        requests.change_priority(mm, z.request_id, priority=MaterialRequest.Priority.URGENT)
        assert allocation(first, z).qty == 5
        assert ledger_of(z).committed == 10
        assert flows(y).pending == 10  # Y gets back what Z gave up
        assert trims("EXCESS_COMMITMENT") == [q(10, 5)]
        assert unallocated(first) == 0


class TestE11:
    def test_a_cut_targets_the_effective_pending(
        self,
        make_line: MakeLine,
        buy: Buy,
        received: FakeReceipts,
        mm: SessionPrincipal,
    ) -> None:
        """Order line 20: W 10 / R 10; 16 received for W (6 surplus): R pends only 4.

        With R approved for 1 (a direct edit), R's excess is 3: the cut sets qty to
        attributed + 1, so pending really drops to 1. Cutting the ceiling to 7 would have
        left it at 4.
        """
        w, r = make_line("10"), make_line("10")
        order = buy((w, "10"), (r, "10"))
        received.receive(only_line(order), "16", {allocation(order, w): "10"})
        assert flows(r).pending == 4
        MaterialRequestLine.objects.filter(pk=r.pk).update(approved_qty=D(1))
        requests.change_needed_by(mm, r.request_id, needed_by=None)  # a no-op: nothing changes
        assert allocation(order, r).qty == 10
        requests.change_priority(mm, r.request_id, priority=MaterialRequest.Priority.LOW)
        assert allocation(order, r).qty == 1
        assert flows(r).pending == 1
        assert trims("EXCESS_COMMITMENT") == [q(10, 1)]


class TestCascade:
    def test_a_cut_hands_quantity_to_a_third_request_that_is_cut_too(
        self,
        make_line: MakeLine,
        buy: Buy,
        received: FakeReceipts,
        delivered: dict[UUID, Decimal],
        mm: SessionPrincipal,
    ) -> None:
        """Order line 40: X 10 / Y 5 / Z 15 / V 10; 25 received at X's site: Y 5, Z 10 and
        V 0 pending; Z's missing 5 and V's 10 bought on other orders. Closing Y gives Z 15
        on the first order (excess 5); cutting Z hands 5 to V (excess 5), which is cut too.
        """
        x, y, z, v = make_line("10"), make_line("5"), make_line("15"), make_line("10")
        first = buy((x, "10"), (y, "5"), (z, "15"), (v, "10"))
        received.receive(only_line(first), "25", {allocation(first, x): "10"})
        delivered[x.pk] = D(10)
        buy((z, "5"))
        buy((v, "10"))
        assert [flows(item).pending for item in (y, z, v)] == [5, 15, 10]
        assert flows(v).pending == 10  # all of it on the second order: V is not over
        requests.close_line_short(mm, y.pk, reason="Not needed")
        assert allocation(first, z).qty == 10
        assert allocation(first, v).qty == 0
        assert [ledger_of(item).committed for item in (z, v)] == [15, 10]
        assert trims("EXCESS_COMMITMENT") == [q(15, 10), q(10, 0)]
        assert unallocated(first) == 5


class TestIssueRevalidation:
    def test_a_draft_over_committed_by_a_direct_edit_is_refused(
        self, make_line: MakeLine, buy: Buy, mm: SessionPrincipal
    ) -> None:
        line = make_line("10")
        draft = buy((line, "10"), issue=False)
        PurchaseOrderLine.objects.filter(po=draft).update(ordered_qty=D(15))
        PurchaseAllocation.objects.filter(po_line__po=draft).update(qty=D(15))
        with pytest.raises(ValidationError) as exc:
            services.issue(mm, draft.pk)
        assert error_codes(exc) == {"over_committed"}
        assert str(line.request.number) in str(exc.value)
        after = PurchaseOrder.objects.get(pk=draft.pk)
        assert (after.status, after.number) == (Status.DRAFT, None)
        assert not DocumentSequence.objects.exists()  # no number was taken
        assert allocation(draft, line).qty == 15  # refused, not trimmed

    def test_a_draft_allocating_more_than_it_orders_is_refused(
        self, make_line: MakeLine, buy: Buy, mm: SessionPrincipal
    ) -> None:
        line = make_line("10")
        draft = buy((line, "10"), issue=False)
        PurchaseOrderLine.objects.filter(po=draft).update(ordered_qty=D(5))
        with pytest.raises(ValidationError) as exc:
            services.issue(mm, draft.pk)
        assert error_codes(exc) == {"over_allocated"}


def _component(org: Org, people: People, cable: Material, size: int) -> PurchaseOrder:
    """A draft line allocated to ``size`` lines of one request: ``size + 1`` nodes."""
    request = MaterialRequest.objects.create(
        project=org.hospital,
        procurement_branch=org.a_mad,
        requested_by=people.tech_mad,
        status=MaterialRequest.Status.APPROVED,
        number="SOL-T-BIG",
        submitted_at=org.hospital.created_at,
    )
    lines = MaterialRequestLine.objects.bulk_create(
        MaterialRequestLine(
            request=request,
            project=org.hospital,
            line_no=number,
            material=cable,
            unit_id=cable.unit_id,
            requested_qty=D(1),
            approved_qty=D(1),
            decision="APPROVED",
            decided_by=people.mm_mad,
            decided_at=request.created_at,
            sourcing="PURCHASE",
        )
        for number in range(1, size + 1)
    )
    order = PurchaseOrder.objects.create(
        branch=org.a_mad,
        buyer_company=org.a,
        supplier=org.supplier,
        ship_to=org.warehouses["A Madrid"],
    )
    po_line = PurchaseOrderLine.objects.create(
        po=order, line_no=1, material=cable, unit_id=cable.unit_id, ordered_qty=D(size)
    )
    PurchaseAllocation.objects.bulk_create(
        PurchaseAllocation(po_line=po_line, request_line=line, qty=D(1)) for line in lines
    )
    return order


class TestBound:
    def test_a_component_above_the_bound_is_refused_cleanly(
        self, org: Org, people: People, cable: Material, mm: SessionPrincipal
    ) -> None:
        order = _component(org, people, cable, components.MAX_NODES)  # 201 nodes
        events = AuditEvent.objects.count()
        with pytest.raises(ValidationError) as exc:
            services.issue(mm, order.pk)
        assert error_codes(exc) == {"component_too_large"}
        request = MaterialRequest.objects.get(number="SOL-T-BIG")
        with pytest.raises(ValidationError) as exc:
            requests.change_priority(mm, request.pk, priority=MaterialRequest.Priority.HIGH)
        assert error_codes(exc) == {"component_too_large"}
        assert PurchaseOrder.objects.get(pk=order.pk).status == Status.DRAFT
        assert MaterialRequest.objects.get(pk=request.pk).priority == "NORMAL"
        assert AuditEvent.objects.count() == events

    def test_a_component_at_the_bound_is_fine(
        self, org: Org, people: People, cable: Material, mm: SessionPrincipal
    ) -> None:
        order = _component(org, people, cable, components.MAX_NODES - 1)  # 200 nodes
        assert services.issue(mm, order.pk).status == Status.ISSUED


class TestSnapshots:
    def test_issued_orders_keep_their_snapshots_after_master_data_edits(
        self, org: Org, make_line: MakeLine, buy: Buy, cable: Material
    ) -> None:
        order = buy((make_line("10"), "10"))
        line = only_line(order)
        kept = (
            order.buyer_snapshot,
            order.supplier_snapshot,
            order.ship_to_snapshot,
            line.material_snapshot,
        )
        assert order.supplier_snapshot["email"] == org.supplier.email
        assert order.buyer_snapshot["company"]["legal_name"] == "Company A"
        assert line.material_snapshot == {"reference": "CAB-25", "name": "Cable 2,5"}
        Supplier.objects.filter(pk=org.supplier.pk).update(
            name="Renamed", email="new@supplier.example", phone="+34 600 000 000"
        )
        Company.objects.filter(pk=org.a.pk).update(legal_name="Company A, renamed")
        Location.objects.filter(pk=order.ship_to_id).update(name="Moved", address="Elsewhere")
        Material.objects.filter(pk=cable.pk).update(name="Cable 2,5 (new name)")
        order.refresh_from_db()
        line.refresh_from_db()
        assert (
            order.buyer_snapshot,
            order.supplier_snapshot,
            order.ship_to_snapshot,
            line.material_snapshot,
        ) == kept
