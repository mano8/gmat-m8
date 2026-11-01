"""Handovers under real concurrency (plan §4.5 I5, §4.7; M6.2 done-when).

- I5: two handovers racing for the last outstanding quantity of a request line: the second
  waits for the component lock, then sees the first and is refused; two that fit both post
  and close the request once;
- a warehouse handover racing a site receipt of the same line: whichever comes second sees
  the first's D, so D never passes A (the receipt keeps the excess as site surplus);
- two voids of one handover: one voids it, the other finds it voided;
- a void racing a new handover: the new one waits, then finds the request reopened.

The first operation is slowed inside its transaction (a subscriber of one of its events
sleeps while its locks are held); the second starts while it waits.
"""

from decimal import Decimal
from typing import Any

import pytest

from apps.access.principals import SessionPrincipal
from apps.audit.models import AuditEvent
from apps.handovers import services
from apps.handovers.models import Handover
from apps.handovers.services import HandoverLineInput, Receiver
from apps.material_requests import ledger
from apps.material_requests.models import MaterialRequest, MaterialRequestLine
from apps.receiving import services as receiving
from apps.receiving.models import GoodsReceipt, GoodsReceiptLine
from apps.receiving.services import ReceiptLineInput
from tests.integration import test_receiving_races as races
from tests.org import Org

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]

# The fixtures and helpers of the receiving races.
approved, buy, mm, slowly = races.approved, races.buy, races.mm, races.slowly
codes, second_starts_later = races.codes, races.second_starts_later

D = Decimal


def hand(principal: SessionPrincipal, org: Org, line: MaterialRequestLine, qty: str) -> Any:
    return services.create_handover(
        principal,
        line.project_id,
        source_location_id=org.warehouses["A Madrid"].pk,
        lines=[HandoverLineInput(line.pk, D(qty))],
        receiver=Receiver(name="Site crew"),
    )


def delivered(line: MaterialRequestLine) -> Decimal:
    return ledger.flows([line.pk])[line.pk].delivered


def test_two_handovers_cannot_exceed_the_approved_quantity(
    org: Org, mm: SessionPrincipal, approved: Any, slowly: Any
) -> None:
    line = approved("10")
    slowly("handover.posted")
    first, second = second_starts_later(
        lambda: hand(mm, org, line, "6"), lambda: hand(mm, org, line, "6")
    )
    assert isinstance(first, Handover)
    assert codes(second) == {"over_delivery"}
    assert delivered(line) == 6


def test_two_handovers_that_fit_close_the_request_once(
    org: Org, mm: SessionPrincipal, approved: Any, slowly: Any
) -> None:
    line = approved("10")
    slowly("handover.posted")
    results = second_starts_later(
        lambda: hand(mm, org, line, "6"), lambda: hand(mm, org, line, "4")
    )
    assert all(isinstance(result, Handover) for result in results)
    assert MaterialRequest.objects.get(pk=line.request_id).status == MaterialRequest.Status.CLOSED
    assert AuditEvent.objects.filter(action="request.auto_closed").count() == 1
    numbers = sorted(Handover.objects.values_list("number", flat=True))
    assert [number[-5:] for number in numbers] == ["00001", "00002"]


def receive_on_site(mm: SessionPrincipal, org: Org, order: Any, qty: str) -> Any:
    return receiving.post_receipt(
        mm,
        order.pk,
        location_id=org.worksite(org.hospital).pk,
        lines=[ReceiptLineInput(order.lines.get().pk, D(qty))],
    )


def test_a_handover_then_a_site_receipt(
    org: Org, mm: SessionPrincipal, approved: Any, buy: Any, slowly: Any
) -> None:
    """The receipt waits, then may attribute only A - D = 4; 2 are site surplus."""
    line = approved("10")
    order = buy((line, "10"))
    slowly("handover.posted")
    handover, receipt = second_starts_later(
        lambda: hand(mm, org, line, "6"), lambda: receive_on_site(mm, org, order, "6")
    )
    assert isinstance(handover, Handover)
    assert isinstance(receipt, GoodsReceipt)
    assert GoodsReceiptLine.objects.get(receipt=receipt).surplus_qty == 2
    assert delivered(line) == 10


def test_a_site_receipt_then_a_handover(
    org: Org, mm: SessionPrincipal, approved: Any, buy: Any, slowly: Any
) -> None:
    line = approved("10")
    order = buy((line, "10"))
    slowly("receipt.posted")
    receipt, handover = second_starts_later(
        lambda: receive_on_site(mm, org, order, "6"), lambda: hand(mm, org, line, "6")
    )
    assert isinstance(receipt, GoodsReceipt)
    assert codes(handover) == {"over_delivery"}
    assert delivered(line) == 6


def test_two_voids_of_one_handover(
    org: Org, mm: SessionPrincipal, approved: Any, slowly: Any
) -> None:
    handover = hand(mm, org, approved("10"), "6")
    slowly("handover.voided")
    first, second = second_starts_later(
        lambda: services.void_handover(mm, handover.pk, reason="Wrong person"),
        lambda: services.void_handover(mm, handover.pk, reason="Wrong person"),
    )
    assert isinstance(first, Handover)
    assert codes(second) == {"transition"}


def test_a_void_racing_a_new_handover(
    org: Org, mm: SessionPrincipal, approved: Any, slowly: Any
) -> None:
    """The new handover waits for the void, then finds the request open and D = 0."""
    line = approved("10")
    handover = hand(mm, org, line, "10")
    slowly("handover.voided")
    voided, again = second_starts_later(
        lambda: services.void_handover(mm, handover.pk, reason="Wrong request"),
        lambda: hand(mm, org, line, "10"),
    )
    assert isinstance(voided, Handover)
    assert isinstance(again, Handover)
    assert delivered(line) == 10
    assert MaterialRequest.objects.get(pk=line.request_id).status == MaterialRequest.Status.CLOSED
