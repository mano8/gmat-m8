"""Dialogs (M8b.3): the two pilots of the htmx form contract, on a phone (a bottom sheet) and on
a desktop (centred). Voiding a receipt is an inline dialog (its form is in the page); changing a
request line is a lazy one (htmx loads the change page's form into it). In both: Esc closes the
dialog and the focus returns to the button that opened it; an invalid form comes back with its
errors in the open dialog, the focus on the summary; a valid one updates the page and shows the
flash. The CSP listener of ``browsing`` fails the test on any violation."""

import re

import pytest
from playwright.sync_api import Locator, Page, expect

from tests.e2e import pages
from tests.e2e.conftest import PHONE, Browsing
from tests.e2e.test_phone_layout import overflow

# Materials no other journey uses, one per viewport, so the ledgers never meet.
VOIDED = {"phone": "CAB-H07VK-6-AZ", "desktop": "CAB-H07VK-6-MA"}
CHANGED = {"phone": "CAB-H07VK-6-GR", "desktop": "CAB-H07VK-6-NE"}


def closes_with_escape(page: Page, dialog: Locator, opener: Locator) -> None:
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    expect(opener).to_be_focused()


def fits(page: Page, dialog: Locator, viewport: str) -> None:
    """On a phone the dialog is a bottom sheet: the full width, on the bottom edge, nothing
    scrolls sideways, and its buttons are 44 px targets. On a desktop it is centred."""
    dialog.evaluate("d => Promise.all(d.getAnimations().map(a => a.finished))")
    box = dialog.bounding_box()
    assert box is not None
    size = page.viewport_size
    assert size is not None
    if viewport == "phone":
        assert box["width"] == PHONE["viewport"]["width"]
        assert abs(box["y"] + box["height"] - size["height"]) <= 1
        assert overflow(page) <= 0
    else:
        assert abs(box["x"] + box["width"] / 2 - size["width"] / 2) <= 1
    for button in dialog.locator("[data-dialog-actions] .btn").all():
        found = button.bounding_box()
        assert found is not None and found["height"] >= 43.5


def comes_back_with(dialog: Locator, error: str) -> None:
    """The form came back in the open dialog: the summary has the focus and names the error."""
    expect(dialog).to_be_visible()
    summary = dialog.locator("[data-error-summary]")
    expect(summary).to_be_focused()
    expect(summary).to_contain_text(error)


@pytest.mark.parametrize("viewport", ["phone", "desktop"])
def test_void_a_receipt_in_a_dialog(browsing: Browsing, viewport: str) -> None:
    material = VOIDED[viewport]
    technician = browsing.as_("tech-mad", viewport)
    number = pages.submit_request(technician, "HOSP-X", {material: 3})
    pages.approve_all(browsing.as_("eng-mad", viewport), number)
    manager = browsing.as_("mm-mad", viewport)
    pages.source_by_purchase(manager, number, [material])
    order = pages.order_from_worklist(
        manager,
        number,
        [material],
        supplier="Suministros Eléctricos Levante SL",
        ship_to="A · Alfa Madrid · Almacén Madrid",
    )
    warehouse = browsing.as_("wo-mad", viewport)
    pages.open_receipt_form(warehouse, order)
    receipt = pages.post_receipt(warehouse, {material: "3"})

    manager.goto(warehouse.url)
    opener = manager.get_by_test_id("void-open")
    dialog = manager.get_by_test_id("void-dialog")
    opener.click()
    expect(dialog).to_be_visible()
    fits(manager, dialog, viewport)
    # A destructive dialog: the way out ("Keep the receipt") has the focus.
    expect(dialog.get_by_test_id("dialog-cancel")).to_be_focused()
    closes_with_escape(manager, dialog, opener)

    opener.click()
    dialog.get_by_test_id("void-submit").click()
    comes_back_with(dialog, "Give a reason.")
    dialog.locator("[name=reason]").fill("Delivered to the wrong site")
    pages.send_dialog(manager, dialog, "void-submit")
    expect(manager.get_by_test_id("void-reason")).to_contain_text("Delivered to the wrong site")
    expect(manager.get_by_text(f"Receipt {receipt} voided.")).to_be_visible()
    expect(manager.get_by_test_id("void-open")).to_have_count(0)


@pytest.mark.parametrize("viewport", ["phone", "desktop"])
def test_change_a_line_in_a_dialog(browsing: Browsing, viewport: str) -> None:
    material = CHANGED[viewport]
    technician = browsing.as_("tech-mad", viewport)
    number = pages.submit_request(technician, "COL-MAD", {material: 5}, reviewer="Elena Navarro")
    engineer = browsing.as_("eng-mad", viewport)
    pages.open_in_inbox(engineer, number)
    examine = engineer.url

    opener = pages.review_line(engineer, material).get_by_test_id("edit-line")
    dialog = pages.open_line_dialog(engineer, material)
    expect(engineer).to_have_url(examine)  # a dialog over the page, not another page
    fits(engineer, dialog, viewport)
    closes_with_escape(engineer, dialog, opener)

    dialog = pages.open_line_dialog(engineer, material)
    form = dialog.get_by_test_id("line-form")
    form.locator("input[name=requested_qty]").fill("4")
    dialog.get_by_test_id("save-edit").click()
    comes_back_with(dialog, "Give a reason.")
    expect(form.locator("input[name=requested_qty]")).to_have_value("4")  # kept as typed
    form.locator("[name=reason]").fill("Four are enough")
    pages.send_dialog(engineer, dialog, "save-edit")
    expect(engineer).to_have_url(re.compile(r"/examine/$"))
    expect(engineer.get_by_text("Line 1 changed.")).to_be_visible()
    expect(pages.review_line(engineer, material)).to_contain_text("Edited")
