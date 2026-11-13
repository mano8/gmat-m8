"""Every role receives at a project's worksite (M9.3, Q44, ADR-0028): the browser layer.

Somebody asks for 7 m of a cable for Hospital X; the engineer approves, the material manager
buys it for A-MAD's warehouse. Then each of the seven business roles (group administrator,
material manager, engineer, warehouse operator, site manager, technician, partner) finds
"Receive materials" on the hospital's page, picks the order among its expected deliveries,
receives 1 m at the worksite and reads the receipt back. Nobody but the requester asked for
the goods, so the technician (phone) and the partner (desktop) receive what someone else
requested. Who reaches the worksite only through the project never meets a price, the
supplier's contact details, the order page or a handover, and every page fits a 360 px phone.
The material manager then voids a project receiver's receipt (a project receiver cannot):
the metre is expected again.
"""

import pytest
from playwright.sync_api import Locator, Page, expect

from apps.core.demo import SUPPLIERS
from tests.e2e import pages
from tests.e2e.conftest import Browsing
from tests.e2e.test_phone_layout import overflow
from tests.e2e.test_styleguide import TARGETS

PROJECT, PLACE = "HOSP-X", "Hospital X"
SUPPLIER = SUPPLIERS[0]
# A price that reads like no quantity or date of these pages, in either notation.
PRICE = "7.4321"
HIDDEN = ("7.43", "7,43", SUPPLIER[3], SUPPLIER[4])
# One cable per viewport, used by no other journey; who asks for it, and which of the two
# project receivers it is someone else's for.
CASES = {
    "phone": ("CAB-H07VK-1,5-AZ", "partner", "tech-mad"),
    "desktop": ("CAB-H07VK-1,5-MA", "tech-mad", "partner"),
}
NAMES = {"partner": "Pablo Cienfuegos", "tech-mad": "Tomás Herrero"}  # the demo people
RECEIVERS = ("admin", "mm-mad", "eng-mad", "wo-mad", "site-manager", "tech-mad", "partner")
SITE_ONLY = ("site-manager", "tech-mad", "partner")
# The site manager reads the handovers of their project (handover.view ○); the others none.
READS_HANDOVERS = ("site-manager",)


def shows_nothing_of_the_order(page: Page, person: str, *also: str) -> None:
    """No price, supplier contact or order page, no handover for who may not read one; nor
    ``also`` (a requester's name)."""
    main = page.locator("main")
    text = str(main.inner_text())
    assert not [hidden for hidden in (*HIDDEN, *also) if hidden in text], page.url
    expect(main.locator("a[href*='/purchasing/']")).to_have_count(0)
    if person not in READS_HANDOVERS:
        expect(main.locator("a[href*='/handovers/']")).to_have_count(0)


def back_to_project(page: Page) -> Locator:
    """The receipt's way back to its project's expected deliveries (the menu has its own)."""
    return page.get_by_test_id("page-header").get_by_role("link", name="Expected deliveries")


# The page's own actions (the app shell's bars are measured by the M8b tests).
ACTIONS = "main a.btn, main button.btn"


def fits(page: Page, viewport: str) -> None:
    """On a phone: no sideways scroll, and every action a 44 px target (M9.2)."""
    if viewport == "phone":
        assert overflow(page) <= 0, f"{page.url} scrolls sideways"
        assert page.evaluate(TARGETS, ACTIONS) == [], f"{page.url}: actions under 44 px"


@pytest.mark.parametrize("viewport", ["phone", "desktop"])
def test_every_role_receives_at_the_project(browsing: Browsing, viewport: str) -> None:
    material, requester, other = CASES[viewport]
    asking = browsing.as_(requester, viewport)
    number = pages.submit_request(asking, PROJECT, {material: 7}, reviewer="Elena Navarro")
    request_page = asking.url
    pages.approve_all(browsing.as_("eng-mad", viewport), number)
    manager = browsing.as_("mm-mad", viewport)
    pages.source_by_purchase(manager, number, [material])
    order = pages.order_from_worklist(
        manager,
        number,
        [material],
        supplier=SUPPLIER[0],
        ship_to="A · Alfa Madrid · Almacén Madrid",
        unit_price=PRICE,
    )

    receipts: dict[str, str] = {}
    receipt_url = ""
    for person in RECEIVERS:
        page = browsing.as_(person, viewport)
        receipts[person] = pages.receive_from_project(page, PROJECT, PLACE, order, {material: "1"})
        expect(page.get_by_test_id("receipt-line")).to_contain_text(material)
        expect(back_to_project(page)).to_be_visible()
        fits(page, viewport)
        if person == other:
            receipt_url = page.url
        if person in SITE_ONLY:
            requester_name = (NAMES[requester],) if person == other else ()
            shows_nothing_of_the_order(page, person, *requester_name)
            expect(page.get_by_test_id("void-open")).to_have_count(0)
            if person in READS_HANDOVERS:  # the direct handover their receipt made
                expect(page.locator("main a[href*='/handovers/']")).to_have_count(1)
            pages.back(page)  # the form they posted
            shows_nothing_of_the_order(page, person, *requester_name)
            fits(page, viewport)
    assert len(set(receipts.values())) == len(RECEIVERS)

    # Everything arrived and went straight to the site: the request is delivered and closed.
    asking.goto(request_page)
    expect(asking.get_by_test_id("page-header")).to_contain_text("Closed")

    # The project receiver reads their receipt back from the project's receipts.
    receiver = browsing.as_(other, viewport)
    receiver.goto("/receiving/")
    expect(receiver.get_by_test_id("receipt-row").filter(has_text=receipts[other])).to_be_visible()
    shows_nothing_of_the_order(receiver, other, NAMES[requester])
    fits(receiver, viewport)
    # The project's page and its "Receive materials", as the receiver finds them.
    receiver.goto("/projects/")
    fits(receiver, viewport)
    receiver.get_by_role("link", name=PROJECT, exact=True).click()
    expect(receiver.get_by_role("link", name="Receive materials")).to_be_visible()
    fits(receiver, viewport)
    # Receiving someone else's goods opens nothing of their request.
    response = receiver.goto(request_page)
    assert response is not None and response.status == 404

    # The order's branch voids it (the receiver could not): one metre is expected again.
    manager.goto(receipt_url)
    manager.get_by_test_id("void-open").click()
    dialog = manager.get_by_test_id("void-dialog")
    dialog.locator("[name=reason]").fill("Counted twice")
    pages.send_dialog(manager, dialog, "void-submit")
    expect(manager.get_by_test_id("void-reason")).to_contain_text("Counted twice")
    receiver.goto(receipt_url)
    expect(receiver.get_by_test_id("void-reason")).to_contain_text("Counted twice")
    back_to_project(receiver).click()
    expected = receiver.get_by_test_id("expected-order").filter(has_text=order)
    expect(expected.get_by_test_id("expected-line").filter(has_text=material)).to_be_visible()
    shows_nothing_of_the_order(receiver, other)
    fits(receiver, viewport)
    asking.goto(request_page)
    expect(asking.get_by_test_id("page-header")).to_contain_text("Approved")
