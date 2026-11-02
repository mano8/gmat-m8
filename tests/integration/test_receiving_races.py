"""Receiving under real concurrency (plan §4.5, §4.7; M6.1 done-when).

- I4: two receipts racing for the last quantity of an order line: the second waits for the
  component lock, then sees the first and is refused; two that fit both post and the order
  closes once;
- two voids of one receipt: one voids it, the other finds it voided;
- I6: a receipt racing a closure of its order line: whichever comes second sees the other,
  so the allocation never ends below what is attributed;
- I13: E13 raced: receipts of the original and the replacement order share one request
  line's receipt capacity A - R, so R never passes A.

The first operation is slowed inside its transaction (a subscriber of one of its events
sleeps while its locks are held); the second starts while it waits.
"""

import time
from collections.abc import Callable, Iterator
from decimal import Decimal
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.access.principals import SessionPrincipal
from apps.catalog.models import Material, UnitOfMeasure
from apps.core import events
from apps.locations.models import Location
from apps.material_requests import ledger
from apps.material_requests.models import MaterialRequest, MaterialRequestLine
from apps.projects.models import Project
from apps.purchasing import services as purchasing
from apps.purchasing.models import PurchaseAllocation, PurchaseOrder
from apps.purchasing.services import RequestItem
from apps.receiving import services
from apps.receiving.models import GoodsReceipt, GoodsReceiptLine
from apps.receiving.services import ReceiptLineInput
from tests.integration.conftest import run_concurrently
from tests.org import Org, People
from tests.support import error_codes, principal_for

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]

D = Decimal
HOLD = 0.4  # seconds the first operation keeps its locks
HEAD_START = 0.15  # seconds before the second operation starts


def second_starts_later(*operations: Callable[[], Any]) -> list[Any]:
    def run(index: int) -> Any:
        if index:
            time.sleep(HEAD_START)
        return operations[index]()

    return run_concurrently(len(operations), run)


def codes(result: Any) -> set[str]:
    assert isinstance(result, ValidationError), result
    return error_codes(result)


@pytest.fixture
def slowly() -> Iterator[Callable[[str], None]]:
    """``slowly(event)``: the publisher of ``event`` holds its locks for ``HOLD``, once."""
    registered: list[Callable[[], None]] = []

    def hold(event: events.Event) -> None:
        for unsubscribe in registered:
            unsubscribe()
        time.sleep(HOLD)

    yield lambda name: registered.append(events.subscribe(name, hold))
    for unsubscribe in registered:
        unsubscribe()


@pytest.fixture
def mm(people: People) -> SessionPrincipal:
    return principal_for(people.mm_mad)


@pytest.fixture
def approved(org: Org, people: People) -> Callable[..., MaterialRequestLine]:
    """An approved line to purchase, one request each, submitted in order."""
    unit, _ = UnitOfMeasure.objects.get_or_create(code="M", defaults={"name": "m", "decimals": 2})
    cable = Material.objects.create(reference="CAB-25", name="Cable 2,5", unit=unit)
    count = iter(range(1, 1000))

    def make(quantity: str, project: Any = None) -> MaterialRequestLine:
        number, now = next(count), timezone.now()
        project = project or org.hospital
        request = MaterialRequest.objects.create(
            project=project,
            procurement_branch=org.a_mad,
            requested_by=people.tech_mad,
            status="APPROVED",
            number=f"SOL-R-{number:05d}",
            submitted_at=now,
            decided_at=now,
        )
        return MaterialRequestLine.objects.create(
            request=request,
            project=project,
            line_no=1,
            material=cable,
            unit=unit,
            requested_qty=D(quantity),
            approved_qty=D(quantity),
            decision="APPROVED",
            decided_by=people.mm_mad,
            decided_at=now,
            sourcing="PURCHASE",
        )

    return make


@pytest.fixture
def buy(org: Org, mm: SessionPrincipal) -> Callable[..., PurchaseOrder]:
    def make(*items: tuple[MaterialRequestLine, str]) -> PurchaseOrder:
        order = purchasing.create_po(
            mm,
            branch_id=org.a_mad.pk,
            supplier_id=org.supplier.pk,
            ship_to_id=org.warehouses["A Madrid"].pk,
        )
        purchasing.add_request_lines(
            mm, order.pk, [RequestItem(line.pk, D(qty)) for line, qty in items]
        )
        purchasing.issue(mm, order.pk)
        return order

    return make


def post(principal: SessionPrincipal, order: PurchaseOrder, qty: str, **kwargs: Any) -> Any:
    line = order.lines.get()
    return services.post_receipt(
        principal,
        order.pk,
        lines=[ReceiptLineInput(line.pk, D(qty))],
        **kwargs,
    )


def test_concurrent_receipts_cannot_exceed_the_ordered_quantity(
    org: Org, mm: SessionPrincipal, approved: Any, buy: Any, slowly: Any
) -> None:
    order = buy((approved("10"), "10"))
    warehouse = org.warehouses["A Madrid"].pk
    slowly("receipt.posted")
    first, second = second_starts_later(
        lambda: post(mm, order, "6", location_id=warehouse),
        lambda: post(mm, order, "6", location_id=warehouse),
    )
    assert isinstance(first, GoodsReceipt)
    assert codes(second) == {"over_receipt"}
    assert sum(GoodsReceiptLine.objects.values_list("qty_received", flat=True)) == 6


def test_two_receipts_that_fit_both_post_and_close_the_order_once(
    org: Org, mm: SessionPrincipal, approved: Any, buy: Any, slowly: Any
) -> None:
    order = buy((approved("10"), "10"))
    warehouse = org.warehouses["A Madrid"].pk
    slowly("receipt.posted")
    results = second_starts_later(
        lambda: post(mm, order, "6", location_id=warehouse),
        lambda: post(mm, order, "4", location_id=warehouse),
    )
    assert all(isinstance(result, GoodsReceipt) for result in results)
    assert PurchaseOrder.objects.get(pk=order.pk).status == PurchaseOrder.Status.CLOSED
    numbers = sorted(GoodsReceipt.objects.values_list("number", flat=True))
    assert [number[-5:] for number in numbers] == ["00001", "00002"]


def test_two_voids_of_one_receipt(
    org: Org, mm: SessionPrincipal, approved: Any, buy: Any, slowly: Any
) -> None:
    order = buy((approved("10"), "10"))
    receipt = post(mm, order, "6", location_id=org.worksite(org.hospital).pk)
    slowly("receipt.voided")
    first, second = second_starts_later(
        lambda: services.void_receipt(mm, receipt.pk, reason="Wrong order"),
        lambda: services.void_receipt(mm, receipt.pk, reason="Wrong order"),
    )
    assert isinstance(first, GoodsReceipt)
    assert codes(second) == {"transition"}


def test_a_receipt_racing_a_closure_of_its_order_line(
    org: Org, mm: SessionPrincipal, approved: Any, buy: Any, slowly: Any
) -> None:
    line = approved("10")
    order = buy((line, "10"))
    slowly("receipt.posted")
    receipt, closed = second_starts_later(
        lambda: post(mm, order, "6", location_id=org.warehouses["A Madrid"].pk),
        lambda: purchasing.close_line_short(mm, order.lines.get().pk, reason="Discontinued"),
    )
    assert isinstance(receipt, GoodsReceipt)
    assert not isinstance(closed, Exception), closed
    allocation = PurchaseAllocation.objects.get()
    assert allocation.qty == 6  # trimmed to what the receipt attributed, never below (I6)
    assert ledger.flows([line.pk])[line.pk].received == 6


def test_receipts_from_two_orders_share_the_receipt_capacity(
    org: Org, mm: SessionPrincipal, approved: Any, buy: Any, slowly: Any
) -> None:
    """E13 raced: the replacement at a warehouse and the original at Z's site, together."""
    third = Project.objects.create(
        code="MAD-2", name="MAD-2", branch=org.a_mad, procurement_branch=org.a_mad
    )
    z_site = Location.objects.create(
        kind=Location.Kind.WORKSITE, branch=org.a_mad, project=third, name="site MAD-2"
    )
    x = approved("10", org.hospital)
    y = approved("10", org.sev_for_mad)
    z = approved("10", third)
    first = buy((x, "10"), (y, "10"), (z, "10"))
    post(mm, first, "15", location_id=org.worksite(org.hospital).pk)  # Y 10 / Z 5 pending
    second = buy((z, "5"))  # Z's missing 5
    slowly("receipt.posted")
    results = second_starts_later(
        lambda: post(mm, second, "5", location_id=org.warehouses["A Madrid"].pk),
        lambda: post(mm, first, "6", location_id=z_site.pk),
    )
    assert all(isinstance(result, GoodsReceipt) for result in results)
    found = ledger.flows([z.pk])[z.pk]
    assert (found.received, found.delivered) == (D(10), D(5))  # never R = 11
