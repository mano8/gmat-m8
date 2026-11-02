"""Reviewer edits against real orders, receipts and handovers (M7b.3, Q36, plan §4.5).

A material change happens in place only while the line has no live allocation (on any
order but a cancelled one, a closed order included) and nothing received or delivered;
otherwise the line is replaced: closed short, its allocations trimmed to what they
received, and an approved line with the new material takes the rest. Quantities never go
below max(D, R) + P + Q, and every edit reconciles the component.

The property test mixes edits with purchases, receipts at a warehouse or on site and
handovers in any order, and checks I1-I13 and C5's material rule after every step, with the
as-submitted copies untouched.
"""

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.db import transaction
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from apps.access.principals import SessionPrincipal
from apps.audit.models import AuditEvent
from apps.catalog.models import Material
from apps.handovers.services import Receiver
from apps.locations.models import Location
from apps.material_requests import ledger, services
from apps.material_requests.models import (
    MaterialRequest,
    MaterialRequestBaseline,
    MaterialRequestBaselineLine,
    MaterialRequestLine,
    MaterialRequestLineRevision,
)
from apps.material_requests.services import LineEdit
from apps.projects.models import Project
from apps.purchasing import receipts
from apps.purchasing import services as purchasing
from apps.purchasing.ledger import LIVE, book_for_order_lines
from apps.purchasing.models import PurchaseAllocation, PurchaseOrder, PurchaseOrderLine
from apps.receiving import services as receiving
from apps.receiving.models import GoodsReceipt
from tests.org import Org, People
from tests.support import error_codes, principal_for
from tests.unit.handovers.conftest import hand_over  # noqa: F401 - fixture
from tests.unit.purchasing.conftest import (  # noqa: F401 - fixtures used by these tests
    Buy,
    MakeLine,
    breaker,
    buy,
    cable,
    make_line,
    mm,
)
from tests.unit.receiving.conftest import Receive, receive, third  # noqa: F401 - fixtures

pytestmark = pytest.mark.django_db

D = Decimal
Kind = MaterialRequestLineRevision.Kind


def fresh(obj: Any) -> Any:
    return type(obj).objects.get(pk=obj.pk)


def flows(line: MaterialRequestLine) -> ledger.Flows:
    return ledger.flows([line.pk])[line.pk]


@pytest.fixture
def engineer(people: People) -> SessionPrincipal:
    """The engineer of A-MAD edits (``request.edit``); A-MAD's material manager buys."""
    return principal_for(people.engineer)


def change_material(by: SessionPrincipal, line: MaterialRequestLine, material: Material) -> Any:
    return services.edit_line(by, line.pk, LineEdit("Substitute", {"material_id": material.pk}))


@pytest.fixture
def cable4(cable: Material) -> Material:  # noqa: F811
    """Another material in metres."""
    return Material.objects.create(reference="CAB-40", name="Cable 4", unit=cable.unit)


def worksite(project: Project) -> Location:
    return Location.objects.get(project=project, kind=Location.Kind.WORKSITE)


class TestInPlaceOrReplace:
    def test_an_allocation_on_an_issued_order_forces_a_replacement(
        self,
        make_line: MakeLine,  # noqa: F811
        buy: Buy,  # noqa: F811
        cable4: Material,
        engineer: SessionPrincipal,
    ) -> None:
        line = make_line("10")
        order = buy((line, "10"))
        revision = change_material(engineer, line, cable4)
        assert revision.kind == Kind.REPLACE
        allocation = PurchaseAllocation.objects.get(request_line=line)
        assert allocation.qty == D(0)  # trimmed to what it received
        trim = AuditEvent.objects.get(action="purchase_allocation.trimmed")
        assert (trim.data["reason"], trim.data["cause"]) == ("REQUEST_LINE_CLOSED", "edit_line")
        new = MaterialRequestLine.objects.get(pk=revision.replacement_line_id)
        assert (new.material_id, new.approved_qty) == (cable4.pk, D(10))
        totals = book_for_order_lines([order.lines.get().pk]).order_line_totals(
            order.lines.get().pk
        )
        assert totals.unallocated == D(10)  # flagged for purchasing

    def test_a_live_allocation_on_a_closed_order_forces_a_replacement(
        self,
        make_line: MakeLine,  # noqa: F811
        buy: Buy,  # noqa: F811
        receive: Receive,  # noqa: F811
        third: Project,  # noqa: F811
        cable4: Material,
        engineer: SessionPrincipal,
    ) -> None:
        """E3: all 20 delivered to X's site; Y keeps an allocation of 10 on the closed
        order with nothing received or delivered. In place, a receipt void would later
        revive pending of the old material on a line asking for another one."""
        x, y = make_line("10", project=third), make_line("10")
        order = buy((x, "10"), (y, "10"))
        receive(order, "20", at=worksite(third))
        assert fresh(order).status == PurchaseOrder.Status.CLOSED
        assert flows(y) == ledger.Flows()  # nothing allocated on draft or issued orders
        revision = change_material(engineer, y, cable4)
        assert revision.kind == Kind.REPLACE
        assert PurchaseAllocation.objects.get(request_line=y).qty == D(0)
        receipt = GoodsReceipt.objects.get(po=order)
        receiving.void_receipt(engineer, receipt.pk, reason="Wrong site")
        assert fresh(order).status == PurchaseOrder.Status.ISSUED
        assert flows(y).pending == D(0)
        new = MaterialRequestLine.objects.get(pk=revision.replacement_line_id)
        assert flows(new) == ledger.Flows()

    def test_an_allocation_trimmed_to_zero_no_longer_counts(
        self,
        make_line: MakeLine,  # noqa: F811
        buy: Buy,  # noqa: F811
        cable4: Material,
        engineer: SessionPrincipal,
    ) -> None:
        line = make_line("10")
        order = buy((line, "4"), issue=False)
        purchasing.unallocate(engineer, PurchaseAllocation.objects.get(request_line=line).pk)
        assert not PurchaseAllocation.objects.filter(LIVE, request_line=line).exists()
        assert change_material(engineer, line, cable4).kind == Kind.CHANGE
        assert fresh(line).material_id == cable4.pk
        assert order.lines.get().material_id != cable4.pk


class TestFloors:
    def test_pending_and_received_bound_the_new_quantity(
        self,
        make_line: MakeLine,  # noqa: F811
        buy: Buy,  # noqa: F811
        receive: Receive,  # noqa: F811
        engineer: SessionPrincipal,
    ) -> None:
        line = make_line("10")
        order = buy((line, "6"))
        receive(order, "2")  # R = 2, P = 4: floor 6
        edit = engineer
        with pytest.raises(ValidationError) as exc:
            services.edit_line(edit, line.pk, LineEdit("x", {"requested_qty": D(5)}))
        assert error_codes(exc) == {"below_commitment"}
        services.edit_line(edit, line.pk, LineEdit("x", {"requested_qty": D(6)}))
        stored = fresh(line)
        assert (stored.requested_qty, stored.approved_qty) == (D(6), D(6))
        assert flows(stored).pending == D(4)
        services.edit_line(edit, line.pk, LineEdit("x", {"requested_qty": D(9)}))
        assert ledger.line_ledgers([fresh(line)])[0].to_purchase == D(3)

    def test_a_replacement_releases_pending_to_the_next_line_and_reconciles(
        self,
        make_line: MakeLine,  # noqa: F811
        buy: Buy,  # noqa: F811
        receive: Receive,  # noqa: F811
        third: Project,  # noqa: F811
        cable4: Material,
        engineer: SessionPrincipal,
    ) -> None:
        """E10 by replacement: Y replaced frees its share of the first order, which the
        waterfall hands to Z, already covered by a second order; the excess is cut."""
        x = make_line("10", project=third)
        y, z = make_line("10"), make_line("10")
        first = buy((x, "10"), (y, "10"), (z, "10"))
        receive(first, "15", at=worksite(third))  # X 10 (+5 surplus); Y 10, Z 5 pending
        buy((z, "5"))
        assert flows(z).pending == D(10)
        change_material(engineer, y, cable4)
        assert flows(z).pending <= D(10)
        z_first = PurchaseAllocation.objects.get(request_line=z, po_line__po=first)
        assert z_first.qty == D(5)
        assert AuditEvent.objects.filter(
            action="purchase_allocation.trimmed", data__reason="EXCESS_COMMITMENT"
        ).exists()


# --- any sequence ---------------------------------------------------------------------


@dataclass
class World:
    """Two approved requests (one line each, for MAD-2 and the hospital) and the means to
    buy for them, receive at A-MAD's warehouse or MAD-2's worksite, hand over and edit."""

    requests: list[MaterialRequest]
    buyer: SessionPrincipal
    editor: SessionPrincipal
    buy: Buy
    receive: Receive
    hand_over: Any
    places: list[Location]
    materials: tuple[Material, Material]


@pytest.fixture
def world(
    org: Org,
    people: People,
    make_line: MakeLine,  # noqa: F811
    buy: Buy,  # noqa: F811
    receive: Receive,  # noqa: F811
    hand_over: Any,  # noqa: F811
    third: Project,  # noqa: F811
    cable4: Material,
) -> World:
    first, second = make_line("10", project=third), make_line("8")
    return World(
        requests=[first.request, second.request],
        buyer=principal_for(people.mm_mad),
        editor=principal_for(people.engineer),
        buy=buy,
        receive=receive,
        hand_over=hand_over,
        places=[org.warehouses["A Madrid"], worksite(third)],
        materials=(Material.objects.get(reference="CAB-25"), cable4),
    )


def latest_live(request: MaterialRequest) -> MaterialRequestLine | None:
    lines = [line for line in request.lines.order_by("-line_no") if ledger.is_live(line)]
    return lines[0] if lines else None


def _buy(world: World, line: MaterialRequestLine, index: int, quantity: int) -> None:
    services.set_sourcing(world.buyer, line.pk, sourcing="PURCHASE")
    world.buy((line, str(quantity)))


def _receive(world: World, line: MaterialRequestLine, index: int, quantity: int) -> None:
    orders = list(PurchaseOrder.objects.filter(status="ISSUED").order_by("pk"))
    if orders:
        order = orders[index % len(orders)]
        world.receive(order, (order.lines.first(), str(quantity)), at=world.places[index])


def _hand_over(world: World, line: MaterialRequestLine, index: int, quantity: int) -> None:
    world.hand_over((line, str(quantity)), receiver=Receiver(name="Crew"))


def _quantity(world: World, line: MaterialRequestLine, index: int, quantity: int) -> None:
    services.edit_line(world.editor, line.pk, LineEdit("q", {"requested_qty": D(quantity)}))


def _material(world: World, line: MaterialRequestLine, index: int, quantity: int) -> None:
    one, other = world.materials
    target = other if line.material_id == one.pk else one
    services.edit_line(world.editor, line.pk, LineEdit("m", {"material_id": target.pk}))


def _remove(world: World, line: MaterialRequestLine, index: int, quantity: int) -> None:
    services.remove_line_as_reviewer(world.editor, line.pk, reason="r")


def _add(world: World, line: MaterialRequestLine, index: int, quantity: int) -> None:
    content = {"material_id": world.materials[0].pk, "requested_qty": D(quantity)}
    services.add_line_as_reviewer(world.editor, line.request_id, LineEdit("a", content))


def _void(world: World, line: MaterialRequestLine, index: int, quantity: int) -> None:
    receipt = GoodsReceipt.objects.filter(status="POSTED").order_by("-pk").first()
    if receipt is not None:
        receiving.void_receipt(world.buyer, receipt.pk, reason="v")


# Each step acts on the latest live line of request ``index`` (the replacement after a
# replacement), when there is one.
STEPS: dict[str, Callable[[World, MaterialRequestLine, int, int], None]] = {
    "buy": _buy,
    "receive": _receive,
    "hand_over": _hand_over,
    "quantity": _quantity,
    "material": _material,
    "remove": _remove,
    "add": _add,
    "void": _void,
}
STEP = st.tuples(st.sampled_from(sorted(STEPS)), st.integers(0, 1), st.integers(1, 12))


def check_invariants(requests: list[MaterialRequest]) -> None:
    """I1, I2, I3, I5, I6, I12, I13 and C5's material rule over everything written."""
    lines = list(MaterialRequestLine.objects.filter(request__in=requests).select_related("request"))
    for entry in ledger.line_ledgers(lines):
        line, flow = entry.line, entry.flows
        approved = line.approved_qty or D(0)
        assert approved <= line.requested_qty  # I1
        assert flow.delivered <= approved and flow.received <= approved  # I5, I13
        assert not entry.is_open or entry.committed <= approved  # I2
    order_lines = list(PurchaseOrderLine.objects.values_list("pk", flat=True))
    book = book_for_order_lines(order_lines)
    for order_line in order_lines:
        totals = book.order_line_totals(order_line)
        assert totals.allocated <= totals.ordered  # I3
        assert totals.pending <= max(totals.remaining, D(0))  # I12
    live = list(PurchaseAllocation.objects.filter(LIVE).select_related("po_line", "request_line"))
    attributed = receipts.attributed([allocation.pk for allocation in live])
    for allocation in live:
        assert attributed.get(allocation.pk, D(0)) <= allocation.qty  # I6
        if book.pending(allocation.pk) > 0:  # C5: what is on order is the line's material
            assert allocation.request_line.material_id == allocation.po_line.material_id


def copies() -> list[tuple[Any, ...]]:
    return list(
        MaterialRequestBaselineLine.objects.order_by("pk").values_list(
            "pk", "line_id", "material_id", "unit_id", "free_text", "requested_qty"
        )
    ) + list(MaterialRequestBaseline.objects.order_by("pk").values_list("pk", "sequence", "notes"))


def run_step(world: World, kind: str, index: int, quantity: int) -> None:
    line = latest_live(fresh(world.requests[index]))
    if line is None:
        return
    try:
        with transaction.atomic():
            STEPS[kind](world, line, index, quantity)
    except ValidationError:
        pass  # a refusal changes nothing; the invariants are checked anyway


def test_invariants_hold_through_any_sequence(world: World) -> None:
    for request in world.requests:  # the copy every later step must leave alone
        line = request.lines.get()
        services.edit_line(world.editor, line.pk, LineEdit("start", {"free_text": "-"}))
    start = copies()

    @settings(
        max_examples=30,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
    )
    @given(steps=st.lists(STEP, max_size=7))
    def run(steps: list[tuple[str, int, int]]) -> None:
        with transaction.atomic():
            for step in steps:
                run_step(world, *step)
                check_invariants(world.requests)
                assert copies() == start
            transaction.set_rollback(True)

    run()
