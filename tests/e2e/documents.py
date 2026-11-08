"""The documents the accessibility and screenshot tests look at (M8b.6).

One flow, made once per run through the screens (as the other journeys do): a request that is
still waiting for its review, a second one approved and sourced for purchase, the order bought for
it and the receiving form of that order. The materials are used by no other journey, so the
ledgers never meet.
"""

from dataclasses import dataclass

from tests.e2e import pages
from tests.e2e.conftest import Browsing

PENDING_MATERIAL = "CAB-H07VK-16-GR"
BOUGHT_MATERIAL = "CAB-H07VK-16-AZ"
SUPPLIER = "Suministros Eléctricos Levante SL"
SHIP_TO = "A · Alfa Madrid · Almacén Madrid"


@dataclass(frozen=True)
class Documents:
    """Addresses of the pages worth auditing, read as the person who works on each."""

    pending_request: str  # the review page of a submitted request (the engineer)
    pending_number: str
    bought_request: str  # the review page of an approved request, sourced for purchase
    request_status: str  # the requester's page of the pending request
    order: str  # the issued order (the material manager)
    order_number: str
    receive: str  # the receiving form of that order (the warehouse operator)


def build(browsing: Browsing) -> Documents:
    technician = browsing.as_("tech-mad", "desktop")
    pending = pages.submit_request(
        technician, "COL-MAD", {PENDING_MATERIAL: 4}, reviewer="Elena Navarro"
    )
    request_status = technician.url
    bought = pages.submit_request(
        technician, "COL-MAD", {BOUGHT_MATERIAL: 8}, reviewer="Elena Navarro"
    )

    engineer = browsing.as_("eng-mad", "desktop")
    pages.open_in_inbox(engineer, pending)
    pending_request = engineer.url
    pages.approve_all(engineer, bought)
    bought_request = engineer.url

    manager = browsing.as_("mm-mad", "desktop")
    pages.source_by_purchase(manager, bought, [BOUGHT_MATERIAL])
    order_number = pages.order_from_worklist(
        manager, bought, [BOUGHT_MATERIAL], supplier=SUPPLIER, ship_to=SHIP_TO
    )
    order = manager.url

    warehouse = browsing.as_("wo-mad", "desktop")
    pages.open_receipt_form(warehouse, order_number)
    return Documents(
        pending_request=pending_request,
        pending_number=pending,
        bought_request=bought_request,
        request_status=request_status,
        order=order,
        order_number=order_number,
        receive=warehouse.url,
    )
