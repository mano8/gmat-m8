"""``expected_deliveries``: the site manager's projection of plan §3.4 (and §8.2's leak rules)."""

from decimal import Decimal

import pytest

from apps.access.context import AccessContext
from apps.access.exceptions import NotInScope
from apps.access.roles import Capability, Role
from apps.accounts.models import User
from apps.catalog.models import Material
from apps.purchasing.models import PurchaseOrder
from apps.receiving import selectors
from tests.org import Org, People, Sentinel, build_sentinel_order, member
from tests.unit.purchasing.conftest import Buy, MakeLine, allocation, only_line
from tests.unit.receiving.conftest import Receive

pytestmark = pytest.mark.django_db

D = Decimal


def ctx(user: User) -> AccessContext:
    return AccessContext.for_user(user)


class TestExpectedDeliveries:
    def test_the_projection(self, org: Org, people: People, make_line: MakeLine, buy: Buy) -> None:
        line = make_line("10")
        order = buy((line, "10"))
        PurchaseOrder.objects.filter(pk=order.pk).update(expected_date="2026-12-01")
        (row,) = selectors.expected_deliveries(ctx(people.site_manager), org.hospital.pk)
        po_line = only_line(order)
        assert (row.po_id, row.po_line_id, row.allocation_id) == (
            order.pk,
            po_line.pk,
            allocation(order, line).pk,
        )
        assert (row.order_number, row.supplier) == (order.number, Sentinel.SUPPLIER_NAME.value)
        assert str(row.expected_date) == "2026-12-01"
        assert (row.material_reference, row.material_name, row.unit) == ("CAB-25", "Cable 2,5", "M")
        assert (row.request_number, row.expected) == (line.request.number, D(10))

    def test_what_is_still_expected(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy, receive: Receive
    ) -> None:
        line = make_line("10")
        order = buy((line, "10"))
        receive(order, "4")  # at the warehouse: 6 still to come
        (row,) = selectors.expected_deliveries(ctx(people.site_manager), org.hospital.pk)
        assert row.expected == 6
        receive(order, "6", at=org.worksite(org.hospital))
        assert selectors.expected_deliveries(ctx(people.site_manager), org.hospital.pk) == []

    def test_a_line_that_can_receive_no_more_expects_nothing(
        self,
        org: Org,
        people: People,
        make_line: MakeLine,
        buy: Buy,
        receive: Receive,
        breaker: Material,
    ) -> None:
        """E3 on one line of two: the hospital's goods all arrived at SEV-2's site."""
        hospital = make_line("10", project=org.hospital)
        sev = make_line("10", project=org.sev_for_mad)
        other = make_line("2", project=org.hospital, material=breaker)
        order = buy((hospital, "10"), (sev, "10"), (other, "2"))
        cables = order.lines.get(material=hospital.material)
        receive(order, (cables, "20"), at=org.worksite(org.sev_for_mad))
        rows = selectors.expected_deliveries(ctx(people.site_manager), org.hospital.pk)
        assert [(row.material_reference, row.expected) for row in rows] == [("DIF-40", D(2))]

    def test_soonest_first(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy, receive: Receive
    ) -> None:
        later, undated, sooner = (buy((make_line("3"), "3")) for _ in range(3))
        PurchaseOrder.objects.filter(pk=later.pk).update(expected_date="2026-12-20")
        PurchaseOrder.objects.filter(pk=sooner.pk).update(expected_date="2026-12-01")
        rows = selectors.expected_deliveries(ctx(people.site_manager), org.hospital.pk)
        assert [row.po_id for row in rows] == [sooner.pk, later.pk, undated.pk]

    def test_nothing_on_order(self, org: Org, people: People) -> None:
        assert selectors.expected_deliveries(ctx(people.site_manager), org.hospital.pk) == []

    def test_never_another_projects_quantity_or_a_price(self, org: Org, people: People) -> None:
        sentinel = build_sentinel_order(org, people)
        rows = selectors.expected_deliveries(ctx(people.site_manager), org.hospital.pk)
        assert [(row.allocation_id, row.expected) for row in rows] == [
            (sentinel.hospital.pk, D(10))
        ]
        text = repr(rows)
        for value in (Sentinel.OTHER_PROJECT_QUANTITY, Sentinel.PRICE, Sentinel.SUPPLIER_EMAIL):
            assert not value.found_in(text)
        assert not Sentinel.SUPPLIER_PHONE.found_in(text)

    @pytest.mark.parametrize(
        "actor",
        ["group_admin", "mm_mad", "wo_mad", "site_manager", "engineer", "tech_mad", "partner"],
    )
    def test_who_may_receive_sees_them(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy, actor: str
    ) -> None:
        buy((make_line("10"), "10"))
        assert len(selectors.expected_deliveries(ctx(getattr(people, actor)), org.hospital.pk)) == 1

    @pytest.mark.parametrize(
        "actor",
        [
            "mm_sev",
            "expired",
            "inactive_partner",
            "inactive_branch",
            "revoked_assignment",
            "nobody",
            "operator",
        ],
    )
    def test_anyone_else_gets_404(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy, actor: str
    ) -> None:
        buy((make_line("10"), "10"))
        with pytest.raises(NotInScope):
            selectors.expected_deliveries(ctx(getattr(people, actor)), org.hospital.pk)

    def test_the_branch_that_executes_the_project_sees_what_it_expects(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy
    ) -> None:
        """B-MAD executes the hospital and A-MAD buys for it: B-MAD's own warehouse operator
        receives on its site (Q44), so it sees what the hospital still expects, though not
        A-MAD's orders themselves (S5)."""
        order = buy((make_line("10"), "10"))
        operator = member(Role.WAREHOUSE_OPERATOR, email="wo-bmad@example.org", branch=org.b_mad)
        rows = selectors.expected_deliveries(ctx(operator), org.hospital.pk)
        assert [(row.po_id, row.expected) for row in rows] == [(order.pk, D(10))]
        assert not PurchaseOrder.objects.visible_to(ctx(operator), Capability.PO_VIEW).exists()
