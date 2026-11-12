"""The reads behind the receiving screens (M6.3): receipts, the order to receive, where goods
may arrive, the receipt form's rows and the expected-deliveries index."""

from decimal import Decimal

import pytest

from apps.access.context import AccessContext
from apps.access.exceptions import NotInScope
from apps.access.principals import SessionPrincipal
from apps.accounts.models import User
from apps.locations.models import Location
from apps.projects.models import Project
from apps.purchasing.selectors import Progress
from apps.receiving import selectors, services
from tests.org import Org, People, Sentinel, build_sentinel_order
from tests.unit.purchasing.conftest import Buy, MakeLine, only_line
from tests.unit.receiving.conftest import Receive

pytestmark = pytest.mark.django_db

D = Decimal


def ctx(user: User) -> AccessContext:
    return AccessContext.for_user(user)


class TestReceipts:
    def test_scope_and_filters(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy, receive: Receive
    ) -> None:
        order = buy((make_line("10"), "10"))
        there = receive(order, "4", delivery_note_ref="DN-77")
        site = receive(order, "2", at=org.worksite(org.hospital))
        assert set(selectors.receipts(ctx(people.wo_mad))) == {there, site}
        assert set(selectors.receipts(ctx(people.site_manager))) == {site}
        assert set(selectors.receipts(ctx(people.mm_sev))) == set()
        everything = selectors.receipts(ctx(people.mm_mad))
        assert set(selectors.receipts(ctx(people.mm_mad), q="dn-77")) == {there}
        assert set(selectors.receipts(ctx(people.mm_mad), q=str(order.number))) == set(everything)
        assert set(selectors.receipts(ctx(people.mm_mad), q=Sentinel.SUPPLIER_NAME.value[:6])) == (
            set(everything)
        )
        assert set(selectors.receipts(ctx(people.mm_mad), q=str(site.number))) == {site}
        assert set(selectors.receipts(ctx(people.mm_mad), status="VOIDED")) == set()
        assert set(selectors.receipts(ctx(people.mm_mad), status="POSTED")) == set(everything)

    def test_one_receipt_or_404(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy, receive: Receive
    ) -> None:
        order = buy((make_line("10"), "10"))
        there = receive(order, "4")
        assert selectors.receipt(ctx(people.wo_mad), there.pk) == there
        for user in (people.site_manager, people.mm_sev, people.tech_mad):
            with pytest.raises(NotInScope):
                selectors.receipt(ctx(user), there.pk)

    def test_who_may_post_or_void(
        self,
        org: Org,
        people: People,
        make_line: MakeLine,
        buy: Buy,
        receive: Receive,
        mm: SessionPrincipal,
    ) -> None:
        order = buy((make_line("10"), "10"))
        there = receive(order, "4")
        assert selectors.can_post(ctx(people.wo_mad)) and selectors.can_post(
            ctx(people.site_manager)
        )
        assert selectors.can_post(ctx(people.tech_mad)) and selectors.can_post(ctx(people.partner))
        assert not selectors.can_post(ctx(people.nobody))
        assert selectors.can_void(ctx(people.mm_mad), there)
        assert not selectors.can_void(ctx(people.wo_mad), there)
        voided = services.void_receipt(mm, there.pk, reason="Wrong delivery")
        assert not selectors.can_void(ctx(people.mm_mad), voided)

    def test_the_lines_with_their_attributions_and_the_direct_handover(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy, receive: Receive
    ) -> None:
        line = make_line("10")
        order = buy((line, "10"))
        site = receive(order, "6", at=org.worksite(org.hospital))
        (row,) = selectors.receipt_lines(ctx(people.site_manager), site)
        assert (row.reference, row.name, row.unit) == ("CAB-25", "Cable 2,5", "M")
        assert row.line.qty_received == 6
        (part,) = row.attributions
        assert (part.request_number, part.project_code, part.qty) == (
            line.request.number,
            org.hospital.code,
            D(6),
        )
        handover = selectors.direct_handover(ctx(people.site_manager), site)
        assert handover is not None and handover.goods_receipt == site
        warehouse = receive(order, "2")
        assert selectors.direct_handover(ctx(people.wo_mad), warehouse) is None
        (only,) = selectors.receipt_lines(ctx(people.wo_mad), warehouse)
        assert [part.qty for part in only.attributions] == [D(2)]

    def test_a_site_manager_sees_only_their_projects_attributions(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy, receive: Receive
    ) -> None:
        hospital, sev = make_line("5"), make_line("5", project=org.sev_for_mad)
        order = buy((hospital, "5"), (sev, "5"))
        site = receive(order, "5", at=org.worksite(org.hospital))
        (row,) = selectors.receipt_lines(ctx(people.site_manager), site)
        assert [part.project_code for part in row.attributions] == [org.hospital.code]


class TestFindingTheOrder:
    def test_orders_of_the_users_branches_found_by_number_supplier_or_project(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy
    ) -> None:
        order = buy((make_line("3"), "3"))
        draft = buy((make_line("3"), "3"), issue=False)
        listed = selectors.receivable_orders(ctx(people.wo_mad))
        assert list(listed) == [order] and draft not in listed
        for query in (
            str(order.number)[-5:],
            "zyxwv",
            org.hospital.code.lower(),
            org.hospital.name[:4],
        ):
            assert list(selectors.receivable_orders(ctx(people.wo_mad), q=query)) == [order]
        assert list(selectors.receivable_orders(ctx(people.wo_mad), q="nothing-like-it")) == []

    def test_a_search_never_repeats_an_order(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy
    ) -> None:
        order = buy((make_line("3"), "3"), (make_line("4", project=org.hospital), "4"))
        found = selectors.receivable_orders(ctx(people.wo_mad), q=org.hospital.code)
        assert list(found) == [order]

    @pytest.mark.parametrize("actor", ["site_manager", "mm_sev", "tech_mad", "partner"])
    def test_others_find_none_by_branch(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy, actor: str
    ) -> None:
        buy((make_line("3"), "3"))
        assert list(selectors.receivable_orders(ctx(getattr(people, actor)))) == []

    def test_progress(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy, receive: Receive
    ) -> None:
        untouched = buy((make_line("3"), "3"))
        partly = buy((make_line("3"), "3"))
        receive(partly, "1")
        done = buy((make_line("3"), "3"))
        receive(done, "3")
        found = selectors.receipt_progress([untouched, partly, done])
        assert found[untouched.pk] is Progress.NOT_RECEIVED
        assert found[partly.pk] is Progress.PARTIAL
        assert found[done.pk] is Progress.RECEIVED
        assert selectors.receipt_progress([]) == {}

    def test_the_order_of_a_branch_or_of_a_project_the_user_sees(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy
    ) -> None:
        order = buy((make_line("3"), "3"))
        for user in (people.wo_mad, people.mm_mad, people.site_manager):
            assert selectors.receipt_order(ctx(user), order.pk) == order
        # Q44: who receives at the hospital's worksite finds an order that buys for it.
        for user in (people.tech_mad, people.partner, people.engineer):
            assert selectors.receipt_order(ctx(user), order.pk) == order

    @pytest.mark.parametrize(
        "actor",
        [
            "mm_sev",
            "expired",
            "inactive_partner",
            "inactive_branch",
            "revoked_assignment",
            "nobody",
        ],
    )
    def test_anyone_else_gets_404(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy, actor: str
    ) -> None:
        order = buy((make_line("3"), "3"))
        with pytest.raises(NotInScope):
            selectors.receipt_order(ctx(getattr(people, actor)), order.pk)

    def test_a_site_manager_does_not_find_an_order_that_buys_nothing_for_their_project(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy
    ) -> None:
        order = buy((make_line("3", project=org.sev_for_mad), "3"))
        with pytest.raises(NotInScope):
            selectors.receipt_order(ctx(people.site_manager), order.pk)

    def test_a_site_manager_finds_issued_and_closed_orders_but_never_a_draft(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy, receive: Receive
    ) -> None:
        draft = buy((make_line("3"), "3"), issue=False)
        with pytest.raises(NotInScope):
            selectors.receipt_order(ctx(people.site_manager), draft.pk)
        assert selectors.receipt_order(ctx(people.wo_mad), draft.pk) == draft  # their branch's
        done = buy((make_line("3"), "3"))
        receive(done, "3")
        done.refresh_from_db()
        assert done.status == "CLOSED"
        assert selectors.receipt_order(ctx(people.site_manager), done.pk) == done

    def test_whole_order_is_for_who_sees_the_order(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy
    ) -> None:
        order = buy((make_line("3"), "3"))
        assert selectors.shows_whole_order(ctx(people.wo_mad), order)
        assert not selectors.shows_whole_order(ctx(people.site_manager), order)


class TestWhereGoodsMayArrive:
    def test_warehouses_and_the_worksites_of_the_projects_served(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy
    ) -> None:
        order = buy((make_line("3"), "3"))
        places = selectors.receipt_locations(ctx(people.wo_mad), order)
        # A-MAD manages B-MAD, whose warehouse is in the order's branch scope (C7).
        assert {place.pk for place in places} == {
            org.warehouses["A Madrid"].pk,
            org.warehouses["B Madrid"].pk,
            org.worksite(org.hospital).pk,
        }

    def test_a_site_manager_may_receive_at_their_worksite_only(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy
    ) -> None:
        order = buy((make_line("3"), "3"))
        places = selectors.receipt_locations(ctx(people.site_manager), order)
        assert [place.pk for place in places] == [org.worksite(org.hospital).pk]

    def test_a_closed_project_and_an_inactive_place_are_left_out(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy
    ) -> None:
        order = buy((make_line("3"), "3"))
        Project.objects.filter(pk=org.hospital.pk).update(status=Project.Status.CLOSED)
        places = selectors.receipt_locations(ctx(people.wo_mad), order)
        assert {place.kind for place in places} == {Location.Kind.WAREHOUSE}
        Location.objects.filter(kind=Location.Kind.WAREHOUSE).update(is_active=False)
        assert selectors.receipt_locations(ctx(people.wo_mad), order) == []

    def test_a_worksite_of_a_project_the_order_does_not_serve_is_left_out(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy
    ) -> None:
        order = buy((make_line("3", project=org.hospital), "3"))
        names = {
            place.project_id for place in selectors.receipt_locations(ctx(people.mm_mad), order)
        }
        assert org.sev_for_mad.pk not in names


class TestTheReceiptFormRows:
    def test_a_warehouse_suggests_everything_outstanding_with_the_default_split(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy, receive: Receive
    ) -> None:
        urgent, normal = make_line("4", priority="URGENT"), make_line("6")
        order = buy((urgent, "4"), (normal, "6"))
        receive(order, "3")
        (row,) = selectors.receipt_rows(ctx(people.wo_mad), order, org.warehouses["A Madrid"])
        assert (row.reference, row.name, row.unit) == ("CAB-25", "Cable 2,5", "M")
        assert (row.remaining, row.suggested) == (D(7), D(7))
        assert [(s.request_number, s.cap, s.suggested) for s in row.splits] == [
            (urgent.request.number, D(1), D(1)),
            (normal.request.number, D(6), D(6)),
        ]

    def test_a_worksite_suggests_what_its_project_can_take(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy
    ) -> None:
        hospital, sev = make_line("4"), make_line("6", project=org.sev_for_mad)
        order = buy((hospital, "4"), (sev, "6"))
        (row,) = selectors.receipt_rows(ctx(people.site_manager), order, org.worksite(org.hospital))
        assert row.remaining is None  # the rest of the order is not for them (§3.4)
        assert row.suggested == D(4)
        assert [(s.project_code, s.cap) for s in row.splits] == [(org.hospital.code, D(4))]
        (whole,) = selectors.receipt_rows(ctx(people.wo_mad), order, org.worksite(org.hospital))
        assert whole.remaining == D(10) and whole.suggested == D(4)

    def test_a_line_nobody_at_the_worksite_waits_for_is_skipped(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy, breaker: object
    ) -> None:
        cables = make_line("4")
        elsewhere = make_line("2", project=org.sev_for_mad, material=breaker)
        order = buy((cables, "4"), (elsewhere, "2"))
        rows = selectors.receipt_rows(ctx(people.site_manager), order, org.worksite(org.hospital))
        assert [row.reference for row in rows] == ["CAB-25"]
        at_warehouse = selectors.receipt_rows(ctx(people.wo_mad), order, org.warehouses["A Madrid"])
        assert {row.reference for row in at_warehouse} == {"CAB-25", "DIF-40"}

    def test_a_line_that_can_receive_no_more_is_skipped(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy, receive: Receive
    ) -> None:
        order = buy((make_line("4"), "4"))
        receive(order, "4")
        assert selectors.receipt_rows(ctx(people.wo_mad), order, org.warehouses["A Madrid"]) == []

    def test_only_an_issued_order_has_rows(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy
    ) -> None:
        draft = buy((make_line("4"), "4"), issue=False)
        assert selectors.receipt_rows(ctx(people.wo_mad), draft, org.warehouses["A Madrid"]) == []

    def test_a_warehouse_line_is_suggested_in_full(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy
    ) -> None:
        line = make_line("4")
        order = buy((line, "4"))
        (row,) = selectors.receipt_rows(ctx(people.wo_mad), order, org.warehouses["A Madrid"])
        assert row.suggested == D(4)

    def test_the_sentinel_order_shows_a_site_manager_nothing_of_the_rest(
        self, org: Org, people: People
    ) -> None:
        sentinel = build_sentinel_order(org, people)
        (row,) = selectors.receipt_rows(
            ctx(people.site_manager), sentinel.order, org.worksite(org.hospital)
        )
        text = repr(row)
        assert row.suggested == D(10) and row.remaining is None
        assert not Sentinel.OTHER_PROJECT_QUANTITY.found_in(text)
        assert not Sentinel.PRICE.found_in(text)
        assert only_line(sentinel.order) == row.po_line


class TestTheExpectedIndex:
    def test_projects_with_a_worksite_the_user_may_receive_at(
        self, org: Org, people: People
    ) -> None:
        assert [p.pk for p in selectors.expected_projects(ctx(people.site_manager))] == [
            org.hospital.pk
        ]
        assert org.hospital.pk in {p.pk for p in selectors.expected_projects(ctx(people.wo_mad))}
        technician = {p.pk for p in selectors.expected_projects(ctx(people.tech_mad))}
        assert org.hospital.pk in technician and org.mad_hold.pk not in technician  # active only
        assert [p.pk for p in selectors.expected_projects(ctx(people.partner))] == [org.hospital.pk]
        assert list(selectors.expected_projects(ctx(people.mm_sev))) != []
        assert org.hospital.pk not in {
            p.pk for p in selectors.expected_projects(ctx(people.mm_sev))
        }
        Project.objects.filter(pk=org.hospital.pk).update(status=Project.Status.CLOSED)
        assert list(selectors.expected_projects(ctx(people.site_manager))) == []

    def test_one_project_or_404(self, org: Org, people: People) -> None:
        assert selectors.expected_project(ctx(people.site_manager), org.hospital.pk) == org.hospital
        with pytest.raises(NotInScope):
            selectors.expected_project(ctx(people.site_manager), org.sev_for_mad.pk)

    def test_the_worksite_if_the_user_may_post_there(self, org: Org, people: People) -> None:
        assert selectors.worksite(ctx(people.site_manager), org.hospital.pk) == org.worksite(
            org.hospital
        )
        assert selectors.worksite(ctx(people.tech_mad), org.hospital.pk) == org.worksite(
            org.hospital
        )
        assert selectors.worksite(ctx(people.mm_sev), org.hospital.pk) is None
        Location.objects.filter(pk=org.worksite(org.hospital).pk).update(is_active=False)
        assert selectors.worksite(ctx(people.site_manager), org.hospital.pk) is None
