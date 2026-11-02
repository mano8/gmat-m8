"""Warehouse availability, a warning only (plan §2.3 point 3, I9).

Received at that warehouse (posted receipts) minus handed over from it (posted
``FROM_WAREHOUSE`` handovers), per material, never below zero; receipts elsewhere do not
count. For whoever may post handovers from that warehouse.
"""

from decimal import Decimal
from typing import Any

import pytest

from apps.access.context import AccessContext
from apps.access.exceptions import NotInScope
from apps.catalog.models import Material, UnitOfMeasure
from apps.handovers import services
from apps.handovers.selectors import Shortfall, stock_warnings, warehouse_stock
from apps.material_requests.models import MaterialRequestLine
from apps.receiving import services as receiving
from tests.org import Org, People
from tests.unit.handovers.conftest import HandOver
from tests.unit.purchasing.conftest import Buy, MakeLine
from tests.unit.receiving.conftest import Receive

pytestmark = pytest.mark.django_db

D = Decimal
STOCK = MaterialRequestLine.Sourcing.STOCK


@pytest.fixture
def ctx(people: People) -> AccessContext:
    return AccessContext.for_user(people.wo_mad)


def free_text_line(line: MaterialRequestLine) -> MaterialRequestLine:
    """A non-catalog line on the same request as ``line``."""
    return MaterialRequestLine.objects.create(
        request_id=line.request_id,
        project=line.project,
        line_no=2,
        free_text="Cinta aislante",
        unit=UnitOfMeasure.objects.get(code="UD"),
        requested_qty=D(5),
        approved_qty=D(5),
        decision=MaterialRequestLine.Decision.APPROVED,
        decided_by=line.decided_by,
        decided_at=line.decided_at,
        sourcing=STOCK,
    )


class TestWarehouseStock:
    def test_received_minus_handed_over(
        self,
        org: Org,
        ctx: AccessContext,
        breaker: Material,
        make_line: MakeLine,
        buy: Buy,
        receive: Receive,
        hand_over: HandOver,
    ) -> None:
        line = make_line("30")
        cable = Material.objects.get(reference="CAB-25").pk  # make_line's default
        order = buy((line, "30"))
        receive(order, "10")
        receive(order, "5", at=org.worksite(org.hospital))  # never entered a warehouse
        hand_over((line, "4"))
        madrid = org.warehouses["A Madrid"]
        assert warehouse_stock(ctx, madrid.pk, [cable, breaker.pk]) == {
            cable: D(6),
            breaker.pk: D(0),
        }
        assert warehouse_stock(ctx, org.warehouses["B Madrid"].pk, [cable]) == {cable: 0}

    def test_voided_documents_do_not_count(
        self,
        org: Org,
        ctx: AccessContext,
        mm: Any,
        make_line: MakeLine,
        buy: Buy,
        receive: Receive,
        hand_over: HandOver,
    ) -> None:
        line = make_line("30")
        cable = Material.objects.get(reference="CAB-25")  # make_line's default
        order = buy((line, "30"))
        kept, voided = receive(order, "10"), receive(order, "3")
        receiving.void_receipt(mm, voided.pk, reason="Wrong order")
        handover = hand_over((line, "4"))
        madrid = org.warehouses["A Madrid"].pk
        assert warehouse_stock(ctx, madrid, [cable.pk]) == {cable.pk: 6}
        services.void_handover(mm, handover.pk, reason="Not taken")
        assert warehouse_stock(ctx, madrid, [cable.pk]) == {cable.pk: 10}
        assert kept.status == "POSTED"

    def test_never_below_zero(
        self,
        org: Org,
        ctx: AccessContext,
        cable: Material,
        make_line: MakeLine,
        hand_over: HandOver,
    ) -> None:
        """Untracked stock went out: nothing is known to be left."""
        hand_over((make_line("10", sourcing=STOCK), "7"))
        assert warehouse_stock(ctx, org.warehouses["A Madrid"].pk, [cable.pk]) == {cable.pk: 0}

    def test_outside_the_scope(self, org: Org, people: People, cable: Material) -> None:
        madrid = org.warehouses["A Madrid"].pk
        for user in (people.tech_mad, people.site_manager, people.mm_sev):
            with pytest.raises(NotInScope):
                warehouse_stock(AccessContext.for_user(user), madrid, [cable.pk])

    def test_a_worksite_has_no_stock(self, org: Org, ctx: AccessContext, cable: Material) -> None:
        with pytest.raises(NotInScope):
            warehouse_stock(ctx, org.worksite(org.hospital).pk, [cable.pk])


class TestStockWarnings:
    def test_the_materials_beyond_the_stock(
        self,
        org: Org,
        ctx: AccessContext,
        cable: Material,
        breaker: Material,
        make_line: MakeLine,
        buy: Buy,
        receive: Receive,
    ) -> None:
        first, second = make_line("10"), make_line("10")
        receive(buy((first, "10")), "6")
        units = make_line("2", material=breaker, sourcing=STOCK)
        madrid = org.warehouses["A Madrid"].pk
        warnings = stock_warnings(ctx, madrid, {first.pk: D(4), second.pk: D(3), units.pk: D(2)})
        assert warnings == [
            Shortfall(cable.pk, "CAB-25", cable.name, "M", D(7), D(6)),
            Shortfall(breaker.pk, "DIF-40", breaker.name, "UD", D(2), D(0)),
        ]
        assert stock_warnings(ctx, madrid, {first.pk: D(4), second.pk: D(2)}) == []

    def test_lines_without_a_figure_are_skipped(
        self, org: Org, people: People, ctx: AccessContext, make_line: MakeLine
    ) -> None:
        line = make_line("10", sourcing=STOCK)
        other = make_line("10", project=org.sev_works, sourcing=STOCK)  # A-SEV's
        madrid = org.warehouses["A Madrid"].pk
        warnings = stock_warnings(ctx, madrid, {free_text_line(line).pk: D(5), other.pk: D(5)})
        assert warnings == []
