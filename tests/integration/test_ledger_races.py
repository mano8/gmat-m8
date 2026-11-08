"""Ledger operations racing on real connections, checked by the independent checker (M8.1).

Each race ends with ``tests.invariants.violations`` finding nothing:

- two issues of one draft: it is numbered and issued once;
- an issue racing a closure of its request line: whichever comes second sees the first,
  so the line ends closed with its allocation trimmed, issued or not;
- discover → lock → verify: a receipt that waits for an amendment adding a line of
  higher priority to its order line finds the grown component under the locks, retries,
  and attributes to the new allocation first;
- a double tap on a receipt with one form key posts it once;
- a receipt void that reopens its order (E8) racing a reschedule of a request on the same
  component: both reconcile, one after the other, so the revived pending is cut once and
  no line ends above its approved quantity.

The first operation is slowed inside its transaction (a subscriber of one of its events
sleeps while its locks are held); the second starts while it waits.
"""

import time
from collections.abc import Callable, Iterator
from decimal import Decimal
from typing import Any

import pytest
from django.core.exceptions import ValidationError

from apps.core import events
from apps.core.idempotency import issue_key
from apps.core.locking import LockSetChanged
from apps.material_requests import ledger
from apps.material_requests import services as requests
from apps.purchasing import components
from apps.purchasing import services as purchasing
from apps.purchasing.models import PurchaseAllocation, PurchaseOrder
from apps.purchasing.services import RequestItem
from apps.receiving import services as receiving
from apps.receiving.models import GoodsReceipt, ReceiptAttribution
from apps.receiving.services import ReceiptLineInput
from tests.integration.conftest import run_concurrently
from tests.invariants import violations
from tests.ledger_world import LedgerWorld, build_ledger_world
from tests.org import Org, People
from tests.support import error_codes

pytestmark = [pytest.mark.integration, pytest.mark.serial, pytest.mark.django_db(transaction=True)]

D = Decimal
HOLD = 0.4  # seconds the first operation keeps its locks
HEAD_START = 0.15  # seconds before the second operation starts


def second_starts_later(*operations: Callable[[], Any]) -> list[Any]:
    def run(index: int) -> Any:
        if index:
            time.sleep(HEAD_START)
        return operations[index]()

    return run_concurrently(len(operations), run)


def outcome(results: list[Any]) -> list[str]:
    return [type(result).__name__ for result in results]


@pytest.fixture
def slowly() -> Iterator[Callable[[str], None]]:
    """``slowly(event)``: the first publisher of ``event`` holds its locks for ``HOLD``."""
    registered: list[Callable[[], None]] = []

    def hold(event: events.Event) -> None:
        for unsubscribe in registered:
            unsubscribe()
        time.sleep(HOLD)

    yield lambda name: registered.append(events.subscribe(name, hold))
    for unsubscribe in registered:
        unsubscribe()


@pytest.fixture
def world(org: Org, people: People) -> LedgerWorld:
    return build_ledger_world(org, people)


def order(world: LedgerWorld, *items: tuple[str, str], issue: bool) -> PurchaseOrder:
    """An order of A-MAD to its warehouse for these lines of the world."""
    po = purchasing.create_po(
        world.buyer,
        branch_id=world.org.a_mad.pk,
        supplier_id=world.org.supplier.pk,
        ship_to_id=world.warehouses["A Madrid"].pk,
    )
    lines = [RequestItem(world.lines[name].pk, D(qty)) for name, qty in items]
    purchasing.add_request_lines(world.buyer, po.pk, lines)
    if issue:
        purchasing.issue(world.buyer, po.pk)
    return PurchaseOrder.objects.get(pk=po.pk)


def allocation(world: LedgerWorld, po: PurchaseOrder, name: str) -> PurchaseAllocation:
    return PurchaseAllocation.objects.get(po_line__po=po, request_line=world.lines[name])


def receive(world: LedgerWorld, po: PurchaseOrder, qty: str, *, at: Any = None, **kw: Any) -> Any:
    return receiving.post_receipt(
        world.buyer,
        po.pk,
        location_id=(at or world.warehouses["A Madrid"]).pk,
        lines=[ReceiptLineInput(po.lines.get().pk, D(qty))],
        **kw,
    )


def assert_holds(world: LedgerWorld) -> None:
    found = violations(world.ctx)
    assert found == [], [str(item) for item in found]


def test_two_issues_of_one_draft_issue_it_once(
    world: LedgerWorld, slowly: Callable[[str], None]
) -> None:
    draft = order(world, ("X", "10"), issue=False)
    slowly("purchase_order.issued")
    results = second_starts_later(
        lambda: purchasing.issue(world.buyer, draft.pk),
        lambda: purchasing.issue(world.buyer, draft.pk),
    )
    assert outcome(results) == ["PurchaseOrder", "ValidationError"]
    assert error_codes(results[1]) == {"transition"}
    issued = PurchaseOrder.objects.get(pk=draft.pk)
    assert (issued.status, issued.number) == ("ISSUED", results[0].number)
    assert ledger.flows([world.lines["X"].pk])[world.lines["X"].pk].pending == 10
    assert_holds(world)


@pytest.mark.parametrize("first", ["issue", "close"])
def test_an_issue_racing_a_closure_of_its_request_line(
    world: LedgerWorld, slowly: Callable[[str], None], first: str
) -> None:
    draft = order(world, ("X", "10"), issue=False)
    x = world.lines["X"].pk
    issue = lambda: purchasing.issue(world.buyer, draft.pk)  # noqa: E731
    close = lambda: requests.close_line_short(world.buyer, x, reason="Not needed")  # noqa: E731
    slowly("purchase_order.issued" if first == "issue" else "request_line.closed_short")
    results = second_starts_later(*((issue, close) if first == "issue" else (close, issue)))
    assert all(not isinstance(result, Exception) for result in results), results
    assert allocation(world, draft, "X").qty == 0  # trimmed, as a draft or once issued
    assert PurchaseOrder.objects.get(pk=draft.pk).status == "ISSUED"
    assert ledger.flows([x])[x] == ledger.Flows()
    assert_holds(world)


def test_a_receipt_whose_component_grows_while_it_waits_is_retried(
    world: LedgerWorld, slowly: Callable[[str], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The receipt discovers X's component (X alone), then waits for its locks while Y
    (HIGH) is added to the same order line; under the locks it finds Y and retries."""
    po = order(world, ("X", "10"), issue=True)
    retries: list[str] = []
    real = components.Component.lock.__func__  # type: ignore[attr-defined]

    def counting(cls: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return real(cls, *args, **kwargs)
        except LockSetChanged:
            retries.append("retry")
            raise

    monkeypatch.setattr(components.Component, "lock", classmethod(counting))
    slowly("purchase_order.amended")
    add = [RequestItem(world.lines["Y"].pk, D(8))]
    results = second_starts_later(
        lambda: purchasing.amend(world.buyer, po.pk, reason="Y too", add=add),
        lambda: receive(world, po, "8"),
    )
    assert outcome(results) == ["PurchaseOrder", "GoodsReceipt"]
    assert retries == ["retry"]
    split = dict(
        ReceiptAttribution.objects.filter(receipt_line__receipt=results[1]).values_list(
            "allocation__request_line_id", "qty"
        )
    )
    assert split == {world.lines["Y"].pk: D(8)}  # the new, higher priority, comes first
    assert_holds(world)


def test_a_double_tap_on_a_receipt_posts_it_once(world: LedgerWorld) -> None:
    po = order(world, ("X", "10"), issue=True)
    key = issue_key(world.buyer.user_id, "receipt.post")
    results = run_concurrently(2, lambda index: receive(world, po, "6", idempotency_key=key))
    assert outcome(results) == ["GoodsReceipt", "GoodsReceipt"]
    assert results[0].pk == results[1].pk
    assert GoodsReceipt.objects.count() == 1
    x = world.lines["X"].pk
    assert ledger.flows([x])[x].received == 6
    assert_holds(world)


@pytest.mark.parametrize("first", ["void", "reschedule"])
def test_a_void_that_reopens_its_order_racing_a_reschedule(
    world: LedgerWorld, slowly: Callable[[str], None], first: str
) -> None:
    """E8: an order line of 16 for X 10 / Z 6, all 16 delivered to X's site (Z's share is
    site surplus), the order closes; Z's 6 are bought again on a second order. Voiding
    the receipt revives Z's pending on the first order, which is cut; a reschedule of Z's
    request reconciles the same component at the same time."""
    first_order = order(world, ("X", "10"), ("Z", "6"), issue=True)
    receipt = receive(world, first_order, "16", at=world.worksite(world.org.hospital.pk))
    assert PurchaseOrder.objects.get(pk=first_order.pk).status == "CLOSED"
    replacement = order(world, ("Z", "6"), issue=True)
    z_request = world.lines["Z"].request_id
    void = lambda: receiving.void_receipt(world.buyer, receipt.pk, reason="Wrong site")  # noqa: E731
    move = lambda: requests.change_priority(world.buyer, z_request, priority="URGENT")  # noqa: E731
    slowly("receipt.voided" if first == "void" else "request.rescheduled")
    results = second_starts_later(*((void, move) if first == "void" else (move, void)))
    assert all(not isinstance(result, ValidationError | Exception) for result in results), results
    assert PurchaseOrder.objects.get(pk=first_order.pk).status == "ISSUED"
    assert allocation(world, first_order, "Z").qty == 0  # its revived pending was cut
    assert allocation(world, replacement, "Z").qty == 6
    x, z = world.lines["X"].pk, world.lines["Z"].pk
    flows = ledger.flows([x, z])
    assert (flows[x].pending, flows[z].pending) == (D(10), D(6))
    assert_holds(world)
