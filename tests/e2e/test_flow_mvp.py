"""The whole MVP flow across roles (plan M8.2): a technician asks for material, an engineer
approves, the material manager buys it, the warehouse and the worksite receive it, the warehouse
hands it over, and the technician sees the request delivered. Once on a phone and once on a
desktop."""

import pytest
from playwright.sync_api import expect

from tests.e2e import pages
from tests.e2e.conftest import Browsing

# One pair of cables per viewport, so the two journeys never meet in the ledger:
# (goes to the warehouse and is handed over, is received on the worksite).
MATERIALS = {
    "phone": ("CAB-H07VK-2,5-AZ", "CAB-H07VK-2,5-GR"),
    "desktop": ("CAB-H07VK-2,5-MA", "CAB-H07VK-2,5-NE"),
}


@pytest.mark.parametrize("viewport", ["phone", "desktop"])
def test_request_to_delivery(browsing: Browsing, viewport: str) -> None:
    stock, site = MATERIALS[viewport]

    technician = browsing.as_("tech-mad", viewport)
    number = pages.submit_request(
        technician,
        "HOSP-X",
        {stock: 6, site: 4},
        priority="High",
        reviewer="Elena Navarro",
    )
    expect(technician.locator("main")).to_contain_text("Submitted")
    request_page = technician.url

    engineer = browsing.as_("eng-mad", viewport)
    pages.approve_all(engineer, number)

    manager = browsing.as_("mm-mad", viewport)
    pages.source_by_purchase(manager, number, [stock, site])
    order = pages.order_from_worklist(
        manager,
        number,
        [stock, site],
        supplier="Suministros Eléctricos Levante SL",
        ship_to="A · Alfa Madrid · Almacén Madrid",
    )

    warehouse = browsing.as_("wo-mad", viewport)
    pages.open_receipt_form(warehouse, order)
    receipt = pages.post_receipt(warehouse, {stock: "6", site: ""}, delivery_note="ALB-E2E")
    expect(warehouse.locator("main")).to_contain_text(stock)

    worksite = browsing.as_("site-manager", viewport)
    site_receipt = pages.receive_at_worksite(worksite, "HOSP-X", "Hospital X", order, {site: "4"})
    assert site_receipt != receipt

    pages.hand_over(warehouse, "HOSP-X", number, {stock: "6"}, receiver="Tomás Herrero")

    technician.goto(request_page)
    expect(technician.locator("main")).to_contain_text("Closed")
    for reference, delivered in ((stock, "6 M"), (site, "4 M")):
        row = technician.get_by_test_id("request-lines").locator("tr", has_text=reference)
        expect(row.locator("td").nth(3)).to_contain_text(delivered)
