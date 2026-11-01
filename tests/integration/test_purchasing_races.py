"""Purchasing under real concurrency (plan §4.5, §4.7; M5.1 done-when: I2, I3 and I6).

- I2: two drafts competing for the same approved quantity reserve it once (E9 raced);
  two closures that each hand order quantity to the same request are serialized by the
  component lock, so the second reconciles what the first left (no line ends over A);
- I3: two allocations racing for the last room on one order line: one is refused;
- I6: an order-line closure and a request-line closure racing over one allocation trim it
  to what is attributed, never below;
- discover → lock → verify: an operation whose component grows while it waits for the
  locks retries and then succeeds.

The first operation is slowed inside its transaction (a subscriber of one of its events
sleeps while its locks are held); the second starts while it waits.
"""

import time
from collections.abc import Callable, Iterator
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from django.utils import timezone

from apps.access.principals import SessionPrincipal
from apps.catalog.models import Material, UnitOfMeasure
from apps.core import events
from apps.core.locking import LockSetChanged
from apps.material_requests import ledger
from apps.material_requests import services as request_services
from apps.material_requests.models import MaterialRequest, MaterialRequestLine
from apps.purchasing import components, receipts, services
from apps.purchasing.ledger import book_for_nodes
from apps.purchasing.models import PurchaseAllocation, PurchaseOrder, PurchaseOrderLine
from apps.purchasing.services import RequestItem
from tests.integration.conftest import run_concurrently
from tests.org import Org, People
from tests.support import error_codes, principal_for
from tests.unit.purchasing.conftest import FakeReceipts

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]

D = Decimal
HOLD = 0.4  # seconds the first operation keeps its locks
HEAD_START = 0.15  # seconds before the second operation starts


def outcome(results: list[Any]) -> list[str]:
    return [type(result).__name__ for result in results]


def second_starts_later(*operations: Callable[[], Any]) -> list[Any]:
    def run(index: int) -> Any:
        if index:
            time.sleep(HEAD_START)
        return operations[index]()

    return run_concurrently(len(operations), run)


@pytest.fixture
def slowly() -> Iterator[Callable[[str], None]]:
    """``slowly(event)``: the publisher of ``event`` holds its locks for ``HOLD``."""
    registered: list[Callable[[], None]] = []

    def hold(event: events.Event) -> None:
        time.sleep(HOLD)

    yield lambda name: registered.append(events.subscribe(name, hold))
    for unsubscribe in registered:
        unsubscribe()


@pytest.fixture
def received() -> Iterator[FakeReceipts]:
    source = FakeReceipts()
    unregister = receipts.register_receipt_source(source)
    yield source
    unregister()


@pytest.fixture
def delivered() -> Iterator[dict[UUID, Decimal]]:
    table: dict[UUID, Decimal] = {}
    unregister = ledger.register_flow_source(
        lambda ids: {i: ledger.Flows(delivered=table[i]) for i in ids if i in table}
    )
    yield table
    unregister()


@pytest.fixture
def cable(db: None) -> Material:
    unit, _ = UnitOfMeasure.objects.get_or_create(code="M", defaults={"name": "m", "decimals": 2})
    return Material.objects.create(reference="CAB-25", name="Cable 2,5", unit=unit)


@pytest.fixture
def mm(people: People) -> SessionPrincipal:
    return principal_for(people.mm_mad)


@pytest.fixture
def approved(org: Org, people: People, cable: Material) -> Callable[[str], MaterialRequestLine]:
    """An approved hospital line to purchase, one request each, submitted in order."""
    count = iter(range(1, 1000))

    def make(quantity: str) -> MaterialRequestLine:
        number = next(count)
        now = timezone.now()
        request = MaterialRequest.objects.create(
            project=org.hospital,
            procurement_branch=org.a_mad,
            requested_by=people.tech_mad,
            status="APPROVED",
            number=f"SOL-T-{number:05d}",
            submitted_at=now.replace(year=2026, month=1, day=1) + timedelta(minutes=number),
            decided_at=now,
        )
        return MaterialRequestLine.objects.create(
            request=request,
            project=org.hospital,
            line_no=1,
            material=cable,
            unit=cable.unit,
            requested_qty=D(quantity),
            approved_qty=D(quantity),
            decision="APPROVED",
            decided_by=people.mm_mad,
            decided_at=now,
            sourcing="PURCHASE",
        )

    return make


def order(
    org: Org, mm: SessionPrincipal, *items: tuple[MaterialRequestLine, str], issue: bool
) -> PurchaseOrder:
    po = services.create_po(
        mm,
        branch_id=org.a_mad.pk,
        supplier_id=org.supplier.pk,
        ship_to_id=org.warehouses["A Madrid"].pk,
    )
    if items:
        services.add_request_lines(mm, po.pk, [RequestItem(line.pk, D(qty)) for line, qty in items])
    if issue:
        services.issue(mm, po.pk)
    return PurchaseOrder.objects.get(pk=po.pk)


def alloc(po: PurchaseOrder, line: MaterialRequestLine) -> PurchaseAllocation:
    return PurchaseAllocation.objects.get(po_line__po=po, request_line=line)


def assert_invariants(delivered: dict[UUID, Decimal]) -> None:
    """I2 for every open line; I3 for every order line; I6 for every allocation."""
    lines = PurchaseOrderLine.objects.values_list("pk", flat=True)
    book = book_for_nodes(
        list(lines), list(MaterialRequestLine.objects.values_list("pk", flat=True))
    )
    assert components.over_committed(book, delivered) == []
    for line_id in book.order_lines:
        assert book.order_line_totals(line_id).allocated <= book.order_lines[line_id].ordered
    for allocation in book.allocations.values():
        assert allocation.attributed <= allocation.qty


def test_two_drafts_reserve_the_same_quantity_once(
    org: Org,
    mm: SessionPrincipal,
    approved: Callable[[str], MaterialRequestLine],
    slowly: Callable[[str], None],
    delivered: dict[UUID, Decimal],
) -> None:
    line = approved("10")
    first, second = order(org, mm, issue=False), order(org, mm, issue=False)
    slowly("purchase_allocation.created")
    results = second_starts_later(
        lambda: services.add_request_lines(mm, first.pk, [RequestItem(line.pk, D(10))]),
        lambda: services.add_request_lines(mm, second.pk, [RequestItem(line.pk, D(10))]),
    )
    assert outcome(results) == ["PurchaseOrder", "ValidationError"]
    assert error_codes(results[1]) == {"exceeds_to_purchase"}
    assert ledger.flows([line.pk])[line.pk].reserved == 10
    assert_invariants(delivered)


def test_two_allocations_race_for_the_last_room_on_a_line(
    org: Org,
    mm: SessionPrincipal,
    approved: Callable[[str], MaterialRequestLine],
    slowly: Callable[[str], None],
    delivered: dict[UUID, Decimal],
) -> None:
    owner, x, y = approved("4"), approved("10"), approved("10")
    draft = order(org, mm, (owner, "4"), issue=False)
    po_line = draft.lines.get()
    services.update_line(mm, po_line.pk, ordered_qty=D(10))  # room for 6 more
    slowly("purchase_allocation.created")
    results = second_starts_later(
        lambda: services.allocate(mm, po_line.pk, request_line_id=x.pk, qty=D(6)),
        lambda: services.allocate(mm, po_line.pk, request_line_id=y.pk, qty=D(6)),
    )
    assert outcome(results) == ["PurchaseAllocation", "ValidationError"]
    assert sum(a.qty for a in po_line.allocations.all()) == 10  # I3
    assert_invariants(delivered)


def test_two_closures_handing_quantity_to_one_request_are_serialized(
    org: Org,
    mm: SessionPrincipal,
    approved: Callable[[str], MaterialRequestLine],
    slowly: Callable[[str], None],
    received: FakeReceipts,
    delivered: dict[UUID, Decimal],
) -> None:
    """Order line 40: X 10 / W 10 / Y 10 / Z 10; 20 received at X's site (X 10 + 10
    surplus): W and Y pend 10 each, Z nothing, so Z's 10 are bought on a second order.
    Closing W and Y at once each hand Z the order line's quantity: whichever comes second
    reconciles what the first left, and Z is never committed above 10."""
    x, w, y, z = approved("10"), approved("10"), approved("10"), approved("10")
    first = order(org, mm, (x, "10"), (w, "10"), (y, "10"), (z, "10"), issue=True)
    received.receive(first.lines.get(), "20", {alloc(first, x): "10"})
    delivered[x.pk] = D(10)
    order(org, mm, (z, "10"), issue=True)
    slowly("request_line.closed_short")
    results = second_starts_later(
        lambda: request_services.close_line_short(mm, w.pk, reason="Not needed"),
        lambda: request_services.close_line_short(mm, y.pk, reason="Not needed"),
    )
    assert outcome(results) == ["MaterialRequestLine", "MaterialRequestLine"]
    assert alloc(first, z).qty == 0  # cut once, by whichever came first
    assert ledger.flows([z.pk])[z.pk].pending == 10
    assert_invariants(delivered)


def test_two_closures_of_one_allocation_stop_at_what_is_attributed(
    org: Org,
    mm: SessionPrincipal,
    approved: Callable[[str], MaterialRequestLine],
    slowly: Callable[[str], None],
    received: FakeReceipts,
    delivered: dict[UUID, Decimal],
) -> None:
    line = approved("10")
    po = order(org, mm, (line, "10"), issue=True)
    received.receive(po.lines.get(), "6", {alloc(po, line): "6"})
    slowly("purchase_allocation.trimmed")
    results = second_starts_later(
        lambda: services.close_line_short(mm, po.lines.get().pk, reason="Supplier ran out"),
        lambda: request_services.close_line_short(mm, line.pk, reason="Enough"),
    )
    assert outcome(results) == ["PurchaseOrderLine", "MaterialRequestLine"]
    assert alloc(po, line).qty == 6  # I6: attributed ≤ qty, trimmed once
    assert_invariants(delivered)


def test_a_component_that_grows_while_waiting_is_retried(
    org: Org,
    mm: SessionPrincipal,
    approved: Callable[[str], MaterialRequestLine],
    slowly: Callable[[str], None],
    monkeypatch: pytest.MonkeyPatch,
    delivered: dict[UUID, Decimal],
) -> None:
    """The schedule change discovers R's component (R alone), then waits for R's locks
    while R is allocated on a draft: under the locks it finds the order and retries."""
    r, s = approved("10"), approved("10")
    draft = order(org, mm, (s, "5"), issue=False)
    changed: list[str] = []
    real = components.Component.lock.__func__  # type: ignore[attr-defined]

    def counting(cls: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return real(cls, *args, **kwargs)
        except LockSetChanged:
            changed.append("retry")
            raise

    monkeypatch.setattr(components.Component, "lock", classmethod(counting))
    slowly("purchase_allocation.created")
    results = second_starts_later(
        lambda: services.add_request_lines(mm, draft.pk, [RequestItem(r.pk, D(4))]),
        lambda: request_services.change_priority(mm, r.request_id, priority="URGENT"),
    )
    assert outcome(results) == ["PurchaseOrder", "MaterialRequest"]
    assert changed == ["retry"]
    assert MaterialRequest.objects.get(pk=r.request_id).priority == "URGENT"
    assert_invariants(delivered)
