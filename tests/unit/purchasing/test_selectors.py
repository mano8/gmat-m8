"""Purchasing reads: scope, the price projection and the purchase worklist (plan §3.4, §4.5).

A warehouse operator sees orders without prices (§8.2); a material manager sees them with
prices; nobody else sees them. The worklist is the material manager's.
"""

from datetime import timedelta
from decimal import Decimal

import pytest

from apps.access.context import AccessContext
from apps.access.exceptions import NotInScope
from apps.access.principals import SessionPrincipal
from apps.catalog.models import Material
from apps.material_requests import services as requests
from apps.material_requests.models import MaterialRequest
from apps.purchasing import selectors, services
from apps.purchasing.models import PurchaseOrder, PurchaseOrderLine
from tests.org import Org, People
from tests.unit.purchasing.conftest import Buy, FakeReceipts, MakeLine, allocation, only_line, q

pytestmark = pytest.mark.django_db

D = Decimal
Status = PurchaseOrder.Status


def ctx(user: object) -> AccessContext:
    return AccessContext.for_user(user)  # type: ignore[arg-type]


class TestOrders:
    def test_who_sees_an_order(self, people: People, buy: Buy, make_line: MakeLine) -> None:
        order = buy((make_line("10"), "10"))
        for user in (people.group_admin, people.mm_mad, people.wo_mad):
            assert list(selectors.orders(ctx(user))) == [order]
            assert selectors.order(ctx(user), order.pk) == order
        for user in (people.mm_sev, people.tech_mad, people.site_manager, people.partner):
            assert not selectors.orders(ctx(user)).exists()
            with pytest.raises(NotInScope):
                selectors.order(ctx(user), order.pk)

    def test_filters(self, org: Org, people: People, buy: Buy, make_line: MakeLine) -> None:
        issued, draft = buy((make_line("10"), "10")), buy(issue=False)
        manager = ctx(people.mm_mad)
        assert list(selectors.orders(manager, status=Status.DRAFT)) == [draft]
        assert list(selectors.orders(manager, supplier_id=org.supplier.pk)) == [draft, issued]


class TestLines:
    def test_prices_only_with_price_view(
        self, people: People, mm: SessionPrincipal, buy: Buy, make_line: MakeLine
    ) -> None:
        order = buy((make_line("10"), "10"), issue=False)
        services.update_line(mm, only_line(order).pk, unit_price=D("2.5"), discount_pct=D("10"))
        (manager,) = selectors.lines(ctx(people.mm_mad), order)
        assert (manager.unit_price, manager.discount_pct, manager.total) == (
            D("2.5"),
            D(10),
            D("22.50"),
        )
        assert selectors.shows_prices(ctx(people.mm_mad), order)
        (operator,) = selectors.lines(ctx(people.wo_mad), order)
        assert (operator.unit_price, operator.discount_pct, operator.total) == (None, None, None)
        assert not selectors.shows_prices(ctx(people.wo_mad), order)
        assert operator.totals.ordered == 10

    def test_no_price_no_total(self, people: People, buy: Buy, make_line: MakeLine) -> None:
        order = buy((make_line("10"), "10"))
        (row,) = selectors.lines(ctx(people.mm_mad), order)
        assert (row.unit_price, row.total) == (None, None)
        assert (row.totals.pending, row.totals.receivable) == (D(10), True)

    def test_issued_lines_show_their_snapshot(
        self, people: People, buy: Buy, make_line: MakeLine, cable: Material
    ) -> None:
        draft = buy((make_line("10"), "5"), issue=False)
        issued = buy((make_line("10"), "5"))
        Material.objects.filter(pk=cable.pk).update(name="Renamed cable")
        (live,) = selectors.lines(ctx(people.mm_mad), draft)
        (kept,) = selectors.lines(ctx(people.mm_mad), issued)
        assert (live.reference, live.name) == ("CAB-25", "Renamed cable")
        assert (kept.reference, kept.name) == ("CAB-25", "Cable 2,5")


class TestAllocations:
    def test_line_by_line_in_attribution_order_trimmed_ones_too(
        self,
        people: People,
        mm: SessionPrincipal,
        buy: Buy,
        make_line: MakeLine,
        breaker: Material,
        received: FakeReceipts,
    ) -> None:
        x, y = make_line("10"), make_line("10")
        units = make_line("3", material=breaker)
        y_request = MaterialRequest.objects.get(pk=y.request_id)
        MaterialRequest.objects.filter(pk=y_request.pk).update(priority="URGENT")
        order = buy((x, "10"), (y, "10"), (units, "3"))
        received.receive(only_line_of(order, 1), "4", {allocation(order, y): "4"})
        requests.close_line_short(mm, units.pk, reason="Not needed")
        rows = selectors.allocations(ctx(people.wo_mad), order)
        assert [(row.request_number, row.request_line_no) for row in rows] == [
            (y.request.number, 1),  # urgent first
            (x.request.number, 1),
            (units.request.number, 1),  # line 2, trimmed to zero
        ]
        assert [(row.attributed, row.ceiling_remaining, row.pending) for row in rows] == [
            q(4, 6, 6),
            q(0, 10, 10),
            q(0, 0, 0),
        ]
        assert {row.project_code for row in rows} == {"HOSP-X"}
        assert rows[2].allocation.qty == 0


def only_line_of(order: PurchaseOrder, number: int) -> PurchaseOrderLine:
    return order.lines.get(line_no=number)


class TestWorklist:
    def test_what_is_left_to_purchase_and_what_drafts_reserve(
        self, org: Org, people: People, buy: Buy, make_line: MakeLine, breaker: Material
    ) -> None:
        open_, reserved, bought = make_line("10"), make_line("10"), make_line("10")
        stock = make_line("10", sourcing="STOCK")
        urgent = make_line("2", material=breaker, priority="URGENT")
        sevilla = make_line("10", project=org.sev_works)  # bought by A-SEV
        buy((reserved, "10"), issue=False)
        buy((bought, "10"))
        buy((open_, "4"), issue=False)
        manager = ctx(people.mm_mad)
        rows = selectors.to_purchase(manager)
        assert [row.line for row in rows] == [urgent, open_, reserved]
        assert [(row.to_purchase, row.reserved, row.committed) for row in rows] == [
            q(2, 0, 0),
            q(6, 4, 4),
            q(0, 10, 10),
        ]
        assert stock not in [row.line for row in rows]
        assert [row.line for row in selectors.to_purchase(ctx(people.mm_sev))] == [sevilla]
        assert selectors.to_purchase(ctx(people.wo_mad)) == []  # no po.manage
        assert [row.line for row in selectors.to_purchase(manager, priority="URGENT")] == [urgent]
        assert [row.line for row in selectors.to_purchase(manager, material_id=breaker.pk)] == [
            urgent
        ]
        assert selectors.to_purchase(manager, project_id=org.sev_for_mad.pk) == []

    def test_unallocated_on_order_is_flagged(
        self,
        people: People,
        mm: SessionPrincipal,
        buy: Buy,
        make_line: MakeLine,
        received: FakeReceipts,
    ) -> None:
        line = make_line("10")
        order = buy((line, "10"))
        buy((make_line("10"), "10"), issue=False)  # drafts are not on order
        assert selectors.unallocated_on_order(ctx(people.mm_mad)) == []
        received.receive(only_line(order), "6", {allocation(order, line): "6"})
        requests.close_line_short(mm, line.pk, reason="Enough")
        (row,) = selectors.unallocated_on_order(ctx(people.mm_mad))
        assert (row.line, row.unallocated) == (only_line(order), D(4))
        assert selectors.unallocated_on_order(ctx(people.wo_mad)) == []

    def test_stale_drafts(self, people: People, buy: Buy) -> None:
        old, recent = buy(issue=False), buy(issue=False)
        PurchaseOrder.objects.filter(pk=old.pk).update(created_at=old.created_at - timedelta(15))
        assert list(selectors.stale_drafts(ctx(people.mm_mad))) == [old]
        assert list(selectors.stale_drafts(ctx(people.mm_mad), days=0)) == [old, recent]
        assert not selectors.stale_drafts(ctx(people.wo_mad)).exists()
