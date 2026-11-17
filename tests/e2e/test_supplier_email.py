"""Sending an issued order to its supplier from its page (M14.4, issue #67).

The stack's ``app.env`` comes from the template, so ``PO_DISPATCH=manual``: issuing emails
nothing, *Send to the supplier* queues the order, the worker delivers it to the mail sink once,
the flash names the address and the panel turns to *Sent* without the page reloading.
"""

import pytest
from playwright.sync_api import expect

from tests.e2e import pages
from tests.e2e.conftest import Browsing
from tests.e2e.documents import SHIP_TO, SUPPLIER
from tests.e2e.mailbox import Mailbox

SUPPLIER_ADDRESS = "pedidos@levante.example"
# A cable no other journey buys, one per viewport, so the ledgers never meet.
MATERIALS = {"phone": "CAB-H07VK-16-VA", "desktop": "CAB-H07VK-10-VA"}


def mails_about(mailbox: Mailbox, number: str) -> list[str]:
    return [
        message["ID"]
        for message in mailbox.messages()
        if number in message.get("Subject", "")
        and any(to["Address"] == SUPPLIER_ADDRESS for to in message.get("To") or [])
    ]


@pytest.mark.parametrize("viewport", ["phone", "desktop"])
def test_an_issued_order_is_sent_by_hand_once(
    browsing: Browsing, mailbox: Mailbox, viewport: str
) -> None:
    material = MATERIALS[viewport]
    technician = browsing.as_("tech-mad", viewport)
    number = pages.submit_request(technician, "COL-MAD", {material: 3}, reviewer="Elena Navarro")
    pages.approve_all(browsing.as_("eng-mad", viewport), number)

    manager = browsing.as_("mm-mad", viewport)
    pages.source_by_purchase(manager, number, [material])
    order = pages.order_from_worklist(
        manager, number, [material], supplier=SUPPLIER, ship_to=SHIP_TO
    )
    # Issued under ``manual``: nothing was queued, and the page said so.
    expect(manager.get_by_test_id("dispatch-empty")).to_contain_text("Not sent by email yet.")
    assert mails_about(mailbox, order) == []

    manager.get_by_test_id("send-open").click()
    dialog = manager.get_by_test_id("send-dialog")
    expect(dialog).to_be_visible()
    expect(dialog.get_by_test_id("send-address")).to_have_text(SUPPLIER_ADDRESS)
    pages.send_dialog(manager, dialog, "send-submit")
    expect(manager.get_by_role("status").filter(has_text=SUPPLIER_ADDRESS)).to_be_visible()

    state = manager.get_by_test_id("dispatch-state").first
    expect(state).to_contain_text(SUPPLIER_ADDRESS)
    manager.evaluate("window.__kept = true")  # from here on the page is not reloaded
    expect(state).to_have_attribute("data-state", "sent", timeout=90_000)
    assert manager.evaluate("window.__kept") is True

    message = mailbox.wait_for(SUPPLIER_ADDRESS, containing=order)
    assert order in message["Subject"]
    assert mails_about(mailbox, order) == [message["ID"]]  # once
