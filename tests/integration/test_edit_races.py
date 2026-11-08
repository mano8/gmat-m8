"""Reviewer edits under real concurrency (M7b.3 done-when; plan §4.7).

- Edit vs decide: the request's header lock (L2) serializes them. A decision sent with the
  version of the page it was made on is refused once an edit landed first, so nobody
  approves a line they have not seen; an edit landing second clears the decision.
- Edit vs receipt: both lock the allocation component (L2-L5). A receipt landing first is
  attributed to the line, which the edit then replaces, keeping what was received; an edit
  landing first closes the line and trims its allocation, so the receipt finds no candidate
  and stores surplus. Either way R ≤ A, attributed ≤ qty and every received unit is
  attributed or surplus.
- The requester's notice: an edit made while the worker holds the waiting notice (its
  claim) does not wait for it; it queues a new one.

The first operation is slowed inside its transaction (a subscriber of one of its events
sleeps while its locks are held); the second starts while it waits.
"""

import time
from collections.abc import Callable, Iterator
from decimal import Decimal
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.access.principals import SessionPrincipal
from apps.catalog.models import Material, UnitOfMeasure
from apps.core import events
from apps.material_requests import ledger, revisions, services
from apps.material_requests.models import MaterialRequest, MaterialRequestLine
from apps.material_requests.services import LineEdit
from apps.notifications.models import EmailOutbox
from apps.purchasing import receipts
from apps.purchasing import services as purchasing
from apps.purchasing.models import PurchaseAllocation, PurchaseOrder
from apps.purchasing.services import RequestItem
from apps.receiving import services as receiving
from apps.receiving.models import GoodsReceipt, GoodsReceiptLine
from apps.receiving.services import ReceiptLineInput
from tests.integration.conftest import run_concurrently
from tests.org import Org, People
from tests.support import error_codes, principal_for

pytestmark = [pytest.mark.integration, pytest.mark.serial, pytest.mark.django_db(transaction=True)]

D = Decimal
HOLD = 0.4  # seconds the first operation keeps its locks
HEAD_START = 0.15  # seconds before the second operation starts
CLAIM_HOLD = 1.5  # seconds the stand-in worker holds the notice it claims


def second_starts_later(*operations: Callable[[], Any]) -> list[Any]:
    def run(index: int) -> Any:
        if index:
            time.sleep(HEAD_START)
        return operations[index]()

    return run_concurrently(len(operations), run)


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
def materials() -> tuple[Material, Material]:
    unit, _ = UnitOfMeasure.objects.get_or_create(code="M", defaults={"name": "m", "decimals": 2})
    return (
        Material.objects.create(reference="CAB-25", name="Cable 2,5", unit=unit),
        Material.objects.create(reference="CAB-40", name="Cable 4", unit=unit),
    )


def submitted(people: People, org: Org, cable: Material, status: str) -> MaterialRequest:
    """A hospital request of A-MAD's technician with one line of 10, through the services
    (so it has its as-submitted copy); approved in full for ``APPROVED``."""
    tech = principal_for(people.tech_mad)
    draft = services.create_draft(tech, project_id=org.hospital.pk)
    services.add_line(tech, draft.pk, material_id=cable.pk, requested_qty=D(10))
    request = services.submit(tech, draft.pk)
    if status == MaterialRequest.Status.APPROVED:
        mm = principal_for(people.mm_mad)
        services.approve_all(mm, request.pk)
        services.set_sourcing(mm, request.lines.get().pk, sourcing="PURCHASE")
    return request


def edit(engineer: SessionPrincipal, line: MaterialRequestLine, **changes: Any) -> Any:
    return services.edit_line(engineer, line.pk, LineEdit("Supplier", changes))


# --- edit vs decide -------------------------------------------------------------------


def test_a_decision_on_a_page_older_than_an_edit_is_refused(
    org: Org, people: People, materials: tuple[Material, Material], slowly: Any
) -> None:
    request = submitted(people, org, materials[0], MaterialRequest.Status.SUBMITTED)
    line = request.lines.get()
    seen = revisions.version(request.pk)
    slowly("request.edited")
    edited, decided = second_starts_later(
        lambda: edit(principal_for(people.engineer), line, requested_qty=D(40)),
        lambda: services.decide_line(
            principal_for(people.mm_mad), line.pk, decision="APPROVED", seen_version=seen
        ),
    )
    assert not isinstance(edited, Exception), edited
    assert isinstance(decided, ValidationError) and error_codes(decided) == {"edited"}
    stored = MaterialRequestLine.objects.get(pk=line.pk)
    assert (stored.requested_qty, stored.decision) == (D(40), "PENDING")
    assert MaterialRequest.objects.get(pk=request.pk).status == "SUBMITTED"


def test_an_edit_landing_after_a_decision_clears_it(
    org: Org, people: People, materials: tuple[Material, Material], slowly: Any
) -> None:
    request = submitted(people, org, materials[0], MaterialRequest.Status.SUBMITTED)
    MaterialRequestLine.objects.create(  # a second line keeps the request submitted
        request=request,
        project=org.hospital,
        line_no=2,
        material=materials[0],
        unit=materials[0].unit,
        requested_qty=D(1),
    )
    line = request.lines.get(line_no=1)
    slowly("request_line.decided")
    decided, edited = second_starts_later(
        lambda: services.decide_line(principal_for(people.mm_mad), line.pk, decision="APPROVED"),
        lambda: edit(principal_for(people.engineer), line, requested_qty=D(40)),
    )
    assert isinstance(decided, MaterialRequestLine), decided
    assert edited.before["decision"] == "APPROVED"
    stored = MaterialRequestLine.objects.get(pk=line.pk)
    assert (stored.requested_qty, stored.decision, stored.approved_qty) == (D(40), "PENDING", None)


# --- edit vs receipt ------------------------------------------------------------------


def bought(org: Org, people: People, request: MaterialRequest) -> PurchaseOrder:
    mm = principal_for(people.mm_mad)
    order = purchasing.create_po(
        mm,
        branch_id=org.a_mad.pk,
        supplier_id=org.supplier.pk,
        ship_to_id=org.warehouses["A Madrid"].pk,
    )
    purchasing.add_request_lines(mm, order.pk, [RequestItem(request.lines.get().pk, D(10))])
    purchasing.issue(mm, order.pk)
    return order


def receive_six(org: Org, people: People, order: PurchaseOrder) -> Any:
    return receiving.post_receipt(
        principal_for(people.mm_mad),
        order.pk,
        location_id=org.warehouses["A Madrid"].pk,
        lines=[ReceiptLineInput(order.lines.get().pk, D(6))],
    )


def check_ledger(order: PurchaseOrder) -> None:
    """R ≤ A per request line, attributed ≤ qty per allocation, and every received unit
    attributed or surplus (I6, I7, I13)."""
    for line in MaterialRequestLine.objects.select_related("request"):
        flow = ledger.flows([line.pk])[line.pk]
        assert flow.received <= (line.approved_qty or D(0))
    allocations = list(PurchaseAllocation.objects.filter(po_line__po=order))
    attributed = receipts.attributed([allocation.pk for allocation in allocations])
    for allocation in allocations:
        assert attributed.get(allocation.pk, D(0)) <= allocation.qty
    for receipt_line in GoodsReceiptLine.objects.all():
        total = sum(
            (part.qty for part in receipt_line.attributions.all()), receipt_line.surplus_qty
        )
        assert total == receipt_line.qty_received


def test_a_receipt_first_stays_on_the_replaced_line(
    org: Org, people: People, materials: tuple[Material, Material], slowly: Any
) -> None:
    request = submitted(people, org, materials[0], MaterialRequest.Status.APPROVED)
    order, line = bought(org, people, request), request.lines.get()
    slowly("receipt.posted")
    receipt, replaced = second_starts_later(
        lambda: receive_six(org, people, order),
        lambda: edit(principal_for(people.engineer), line, material_id=materials[1].pk),
    )
    assert isinstance(receipt, GoodsReceipt), receipt
    assert replaced.kind == "REPLACE"
    assert ledger.flows([line.pk])[line.pk].received == D(6)
    assert PurchaseAllocation.objects.get(request_line=line).qty == D(6)
    new = MaterialRequestLine.objects.get(pk=replaced.replacement_line_id)
    assert new.approved_qty == D(4)  # the rest: A - max(D, R)
    check_ledger(order)


def test_an_edit_first_turns_the_receipt_into_surplus(
    org: Org, people: People, materials: tuple[Material, Material], slowly: Any
) -> None:
    request = submitted(people, org, materials[0], MaterialRequest.Status.APPROVED)
    order, line = bought(org, people, request), request.lines.get()
    slowly("request.edited")
    replaced, receipt = second_starts_later(
        lambda: edit(principal_for(people.engineer), line, material_id=materials[1].pk),
        lambda: receive_six(org, people, order),
    )
    assert replaced.kind == "REPLACE", replaced
    assert isinstance(receipt, GoodsReceipt), receipt
    assert ledger.flows([line.pk])[line.pk].received == D(0)
    assert GoodsReceiptLine.objects.get().surplus_qty == D(6)
    new = MaterialRequestLine.objects.get(pk=replaced.replacement_line_id)
    assert new.approved_qty == D(10)
    check_ledger(order)


# --- the notice and the worker's claim ------------------------------------------------


def test_an_edit_never_waits_for_the_workers_claim(
    org: Org, people: People, materials: tuple[Material, Material]
) -> None:
    request = submitted(people, org, materials[0], MaterialRequest.Status.APPROVED)
    line = request.lines.get()
    engineer = principal_for(people.engineer)
    edit(engineer, line, requested_qty=D(11))
    (waiting,) = EmailOutbox.objects.filter(source_ref=f"request.edited:{request.pk}")

    def claim_and_hold() -> None:
        with transaction.atomic():
            EmailOutbox.objects.select_for_update().get(pk=waiting.pk)
            time.sleep(CLAIM_HOLD)

    def edit_again() -> float:
        started = time.monotonic()
        edit(engineer, line, requested_qty=D(12))
        return time.monotonic() - started

    _held, took = second_starts_later(claim_and_hold, edit_again)
    assert isinstance(took, float), took
    assert took < CLAIM_HOLD - HEAD_START - 0.5  # it would wait about 1.35 s
    assert EmailOutbox.objects.filter(source_ref=f"request.edited:{request.pk}").count() == 2
    assert timezone.now() < max(EmailOutbox.objects.values_list("not_before", flat=True))
