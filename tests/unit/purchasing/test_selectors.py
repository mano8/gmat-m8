"""Purchasing reads: scope, the price projection and the purchase worklist (plan §3.4, §4.5).

A warehouse operator sees orders without prices (§8.2); a material manager sees them with
prices; nobody else sees them. The worklist is the material manager's.
"""

from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone, translation

from apps.access.context import AccessContext
from apps.access.exceptions import NotInScope
from apps.access.principals import SessionPrincipal
from apps.catalog.models import Material
from apps.locations.models import Location
from apps.material_requests import services as request_services
from apps.material_requests.models import MaterialRequest
from apps.projects.models import Project
from apps.purchasing import selectors, services
from apps.purchasing.models import PurchaseOrder, PurchaseOrderLine
from tests.org import Org, People
from tests.unit.purchasing.conftest import Buy, FakeReceipts, MakeLine, allocation, only_line, q

pytestmark = pytest.mark.django_db

D = Decimal
Status = PurchaseOrder.Status


@pytest.fixture(autouse=True)
def english() -> Iterator[None]:
    """Labels are asserted in English (the default language is Spanish)."""
    with translation.override("en"):
        yield


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
        request_services.close_line_short(mm, units.pk, reason="Not needed")
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
        request_services.close_line_short(mm, line.pk, reason="Enough")
        (row,) = selectors.unallocated_on_order(ctx(people.mm_mad))
        assert (row.line, row.unallocated) == (only_line(order), D(4))
        assert selectors.unallocated_on_order(ctx(people.wo_mad)) == []

    def test_stale_drafts(self, people: People, buy: Buy) -> None:
        old, recent = buy(issue=False), buy(issue=False)
        PurchaseOrder.objects.filter(pk=old.pk).update(created_at=old.created_at - timedelta(15))
        assert list(selectors.stale_drafts(ctx(people.mm_mad))) == [old]
        assert list(selectors.stale_drafts(ctx(people.mm_mad), days=0)) == [old, recent]
        assert not selectors.stale_drafts(ctx(people.wo_mad)).exists()


class TestWorklistByMaterial:
    def test_groups_the_lines_of_a_material(
        self, people: People, make_line: MakeLine, breaker: Material
    ) -> None:
        first, second, units = make_line("10"), make_line("5"), make_line("2", material=breaker)
        groups = selectors.by_material(selectors.to_purchase(ctx(people.mm_mad)))
        assert [(g.name, g.reference, g.unit, g.to_purchase) for g in groups] == [
            ("Cable 2,5", "CAB-25", "M", D(15)),
            ("Diferencial 40A", "DIF-40", "UD", D(2)),
        ]
        assert [row.line for row in groups[0].rows] == [first, second]
        assert groups[1].rows[0].line == units

    def test_the_worklist_narrows_to_a_branch_and_to_chosen_lines(
        self, org: Org, people: People, make_line: MakeLine
    ) -> None:
        mine, sev = make_line("10"), make_line("10", project=org.sev_for_mad)
        both = ctx(people.group_admin)
        assert {row.line for row in selectors.to_purchase(both, branch_id=org.a_mad.pk)} == {
            mine,
            sev,
        }
        assert selectors.to_purchase(both, branch_id=org.a_sev.pk) == []
        assert [row.line for row in selectors.to_purchase(both, line_ids=[mine.pk])] == [mine]

    def test_unallocated_and_drafts_narrow_to_a_supplier(
        self, org: Org, people: People, buy: Buy
    ) -> None:
        draft = buy(issue=False)
        manager = ctx(people.mm_mad)
        assert list(selectors.drafts(manager)) == [draft]
        assert list(selectors.drafts(manager, supplier_id=org.supplier.pk)) == [draft]
        assert not selectors.drafts(manager, supplier_id=org.a.pk).exists()
        assert selectors.unallocated_on_order(manager, supplier_id=org.supplier.pk) == []

    def test_unallocated_narrows_to_a_supplier(
        self,
        org: Org,
        people: People,
        mm: SessionPrincipal,
        buy: Buy,
        make_line: MakeLine,
        received: FakeReceipts,
    ) -> None:
        line = make_line("10")
        order = buy((line, "10"))
        received.receive(only_line(order), "6", {allocation(order, line): "6"})
        request_services.close_line_short(mm, line.pk, reason="Enough")
        manager = ctx(people.mm_mad)
        assert len(selectors.unallocated_on_order(manager, supplier_id=org.supplier.pk)) == 1
        assert selectors.unallocated_on_order(manager, supplier_id=org.a.pk) == []

    def test_staleness(self, buy: Buy, make_line: MakeLine) -> None:
        draft, issued = buy(issue=False), buy((make_line("10"), "10"))
        assert not selectors.is_stale(draft)
        PurchaseOrder.objects.filter(pk=draft.pk).update(created_at=timezone.now() - timedelta(15))
        draft.refresh_from_db()
        assert selectors.is_stale(draft) and not selectors.is_stale(draft, days=30)
        assert not selectors.is_stale(issued, days=0)  # only drafts go stale

    def test_projects_to_filter_by(self, org: Org, people: People) -> None:
        codes = {p.code for p in selectors.worklist_projects(ctx(people.mm_mad))}
        assert "HOSP-X" in codes and "SEV-1" not in codes
        assert not selectors.worklist_projects(ctx(people.wo_mad)).exists()


class TestBuilderChoices:
    def test_destinations_are_the_branchs_warehouses_and_the_worksites_it_buys_for(
        self, org: Org, people: People
    ) -> None:
        found = set(selectors.ship_to_choices(ctx(people.mm_mad), branch_id=org.a_mad.pk))
        assert org.warehouses["A Madrid"] in found
        assert org.worksite(org.hospital) in found  # A-MAD buys for the hospital
        assert org.worksite(org.sev_for_mad) in found  # and for SEV-2
        assert org.warehouses["A Sevilla"] not in found
        assert org.worksite(org.sev_works) not in found  # A-SEV buys for it

    def test_narrowing_to_a_branch_and_to_the_users_scope(self, org: Org, people: People) -> None:
        sev = set(selectors.ship_to_choices(ctx(people.group_admin), branch_id=org.a_sev.pk))
        assert org.warehouses["A Sevilla"] in sev and org.warehouses["A Madrid"] not in sev
        assert not selectors.ship_to_choices(ctx(people.wo_mad)).exists()  # no po.manage

    def test_an_inactive_project_or_location_is_not_offered(self, org: Org, people: People) -> None:
        Project.objects.filter(pk=org.hospital.pk).update(status="ON_HOLD")
        found = set(selectors.ship_to_choices(ctx(people.mm_mad), branch_id=org.a_mad.pk))
        assert org.worksite(org.hospital) not in found
        Location.objects.filter(pk=org.warehouses["A Madrid"].pk).update(is_active=False)
        found = set(selectors.ship_to_choices(ctx(people.mm_mad), branch_id=org.a_mad.pk))
        assert org.warehouses["A Madrid"] not in found

    def test_candidates_are_what_the_order_could_still_take(
        self, org: Org, people: People, buy: Buy, make_line: MakeLine
    ) -> None:
        taken, free = make_line("10"), make_line("5")
        elsewhere = make_line("5", project=org.sev_works)
        order = buy((taken, "4"), issue=False)
        manager = ctx(people.mm_mad)
        found = {row.line for row in selectors.candidates(manager, order)}
        assert found == {free}  # `taken` is on the order; `elsewhere` is bought by A-SEV
        assert elsewhere not in found

    def test_a_worksite_order_takes_only_its_projects_lines(
        self, org: Org, people: People, buy: Buy, make_line: MakeLine
    ) -> None:
        hospital, sev = make_line("5"), make_line("5", project=org.sev_for_mad)
        order = buy(issue=False, ship_to=org.worksite(org.hospital))
        found = {row.line for row in selectors.candidates(ctx(people.mm_mad), order)}
        assert found == {hospital} and sev not in found

    def test_a_line_fully_to_buy_elsewhere_is_not_a_candidate(
        self, people: People, buy: Buy, make_line: MakeLine
    ) -> None:
        reserved = make_line("5")
        buy((reserved, "5"), issue=False)  # reserved by another draft: nothing to purchase
        order = buy(issue=False)
        assert selectors.candidates(ctx(people.mm_mad), order) == []


class TestProgress:
    def test_nothing_partly_and_fully_received(
        self,
        people: People,
        buy: Buy,
        make_line: MakeLine,
        received: FakeReceipts,
    ) -> None:
        line = make_line("10")
        order = buy((line, "10"))
        manager = ctx(people.mm_mad)
        assert selectors.progress(manager, [order]) == {order.pk: selectors.Progress.NOT_RECEIVED}
        received.receive(only_line(order), "4", {allocation(order, line): "4"})
        assert selectors.progress(manager, [order])[order.pk] == selectors.Progress.PARTIAL
        received.receive(only_line(order), "6", {allocation(order, line): "6"})
        assert selectors.progress(manager, [order])[order.pk] == selectors.Progress.RECEIVED
        assert str(selectors.Progress.PARTIAL.label) == "Partly received"
        assert {str(p.label) for p in selectors.Progress} == {
            "Not received",
            "Partly received",
            "Received",
        }

    def test_an_order_without_visible_lines_has_no_progress(
        self, people: People, buy: Buy, make_line: MakeLine
    ) -> None:
        order = buy((make_line("10"), "10"))
        assert selectors.progress(ctx(people.mm_sev), [order]) == {}


class TestLookups:
    def test_a_line_and_an_allocation_in_scope(
        self, people: People, buy: Buy, make_line: MakeLine
    ) -> None:
        line = make_line("10")
        order = buy((line, "10"))
        po_line, alloc = only_line(order), allocation(order, line)
        assert selectors.order_line(ctx(people.wo_mad), po_line.pk) == po_line
        assert selectors.allocation(ctx(people.wo_mad), alloc.pk) == alloc
        with pytest.raises(NotInScope):
            selectors.order_line(ctx(people.mm_sev), po_line.pk)
        with pytest.raises(NotInScope):
            selectors.allocation(ctx(people.mm_sev), alloc.pk)

    def test_who_may_manage(self, people: People, buy: Buy) -> None:
        order = buy(issue=False)
        assert selectors.can_manage(ctx(people.mm_mad)) and selectors.can_manage(
            ctx(people.mm_mad), order
        )
        assert not selectors.can_manage(ctx(people.wo_mad))
        assert not selectors.can_manage(ctx(people.mm_sev), order)


class TestHistory:
    def test_revisions_closures_and_trims_oldest_first(
        self,
        people: People,
        mm: SessionPrincipal,
        buy: Buy,
        make_line: MakeLine,
        received: FakeReceipts,
    ) -> None:
        line = make_line("10")
        order = buy((line, "10"))
        services.amend(
            mm, order.pk, reason="Quote changed", header=services.HeaderAmendment(notes="note")
        )
        request_services.close_line_short(mm, line.pk, reason="Enough")
        services.close_line_short(mm, only_line(order).pk, reason="Supplier short")
        entries = selectors.history(ctx(people.mm_mad), order)
        labels = [str(entry.label) for entry in entries]
        assert labels[0] == "Draft created" and "Issued" in labels and "Amended" in labels
        assert "Allocation trimmed" in labels and "Line closed short" in labels
        details = {str(entry.label): entry.detail for entry in entries}
        assert "Quote changed" in details["Amended"] and "#1" in details["Amended"]
        assert "Supplier short" in details["Line closed short"]
        assert "10 → 0" in details["Allocation trimmed"]
        assert "request line was closed short" in details["Allocation trimmed"]
        assert all(entry.actor for entry in entries)

    def test_only_who_manages_or_audits_reads_it(
        self, people: People, buy: Buy, make_line: MakeLine
    ) -> None:
        order = buy((make_line("10"), "10"))
        assert selectors.history(ctx(people.mm_mad), order)
        assert selectors.history(ctx(people.group_admin), order)
        assert selectors.history(ctx(people.wo_mad), order) == []
