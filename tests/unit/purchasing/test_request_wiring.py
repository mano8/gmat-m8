"""The request services against real purchase orders (M4.2 seams wired in M5.1).

The M4 tests fake purchasing's flows; here the floors and checks of the request services
see real allocations, and their closures trim them (plan §4.4, §4.5, §4.9).
"""

from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse

from apps.access.context import AccessContext
from apps.access.principals import SessionPrincipal
from apps.audit.models import AuditEvent
from apps.material_requests import selectors as request_selectors
from apps.material_requests import services as requests
from apps.material_requests.models import MaterialRequest
from apps.purchasing import services
from tests.org import Org, People, client_for
from tests.support import error_codes, principal_for
from tests.unit.purchasing.conftest import Buy, FakeReceipts, MakeLine, allocation, flows, only_line

pytestmark = pytest.mark.django_db

D = Decimal


def trimmed(reason: str) -> list[tuple[str, str]]:
    events = AuditEvent.objects.filter(action="purchase_allocation.trimmed").order_by("id")
    return [(e.data["from"], e.data["to"]) for e in events if e.data["reason"] == reason]


def test_a_manual_close_trims_and_a_reopen_does_not_revive(
    mm: SessionPrincipal, buy: Buy, make_line: MakeLine, received: FakeReceipts
) -> None:
    line = make_line("10")
    order = buy((line, "10"))
    received.receive(only_line(order), "3", {allocation(order, line): "3"})
    requests.manual_close(mm, line.request_id, reason="Project scaled down")
    assert allocation(order, line).qty == 3
    assert trimmed("REQUEST_CLOSED") == [("10.000", "3")]
    requests.reopen(mm, line.request_id, reason="Back on")
    assert MaterialRequest.objects.get(pk=line.request_id).status == "APPROVED"
    found = flows(line)
    assert (found.received, found.pending) == (D(3), D(0))


def test_the_reduction_floor_counts_what_is_on_order(
    mm: SessionPrincipal, buy: Buy, make_line: MakeLine
) -> None:
    line = make_line("10")
    buy((line, "4"))
    buy((line, "2"), issue=False)
    with pytest.raises(ValidationError) as exc:
        requests.reduce_approved_qty(mm, line.pk, approved_qty=D(5))
    assert error_codes(exc) == {"below_commitment"}
    requests.reduce_approved_qty(mm, line.pk, approved_qty=D(6))


def test_nothing_is_cancelled_or_moved_to_stock_while_on_order(
    mm: SessionPrincipal, buy: Buy, make_line: MakeLine
) -> None:
    line = make_line("10")
    draft = buy((line, "4"), issue=False)
    with pytest.raises(ValidationError) as exc:
        requests.set_sourcing(mm, line.pk, sourcing="STOCK")
    assert error_codes(exc) == {"purchase_in_progress"}
    with pytest.raises(ValidationError) as exc:
        requests.cancel(mm, line.request_id, reason="Not needed")
    assert error_codes(exc) == {"in_progress"}
    services.cancel(mm, draft.pk)  # the reservation is gone: both are possible again
    requests.set_sourcing(mm, line.pk, sourcing="STOCK")
    assert requests.cancel(mm, line.request_id, reason="Not needed").status == "CANCELLED"


def test_a_manual_close_warns_when_an_order_ships_to_the_requests_own_worksite(
    org: Org, people: People, buy: Buy, make_line: MakeLine
) -> None:
    line = make_line("10")
    manager = AccessContext.for_user(people.mm_mad)
    request = MaterialRequest.objects.get(pk=line.request_id)
    warehouse = buy((line, "4"))  # an order to the warehouse: no warning
    assert not request_selectors.ships_to_own_worksite(manager, request)
    services.cancel(principal_for(people.mm_mad), warehouse.pk, reason="Wrong destination")
    draft = buy((line, "4"), issue=False, ship_to=org.worksite(org.hospital))
    assert not request_selectors.ships_to_own_worksite(manager, request)  # not issued yet
    services.issue(principal_for(people.mm_mad), draft.pk)
    assert request_selectors.ships_to_own_worksite(manager, request)
    assert not request_selectors.ships_to_own_worksite(
        AccessContext.for_user(people.wo_mad), request
    )
    page = client_for(people.mm_mad).get(reverse("requests:examine", args=[request.pk]))
    assert 'data-testid="worksite-warning"' in page.content.decode()
