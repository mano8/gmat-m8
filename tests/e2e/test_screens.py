"""The screens of M8b.4 in a browser, on a phone and on a desktop.

The review page's actions are dialogs opened from its menu (the main action, approve all, sits in
the sticky bar); a role's validity is a lazy dialog of the person page; a list's filters open in
a sheet on a phone and show as removable chips. Nothing scrolls sideways at 360 px, an open dialog
included, and the CSP listener of ``browsing`` fails any test on a violation.
"""

import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e import pages
from tests.e2e.conftest import Browsing
from tests.e2e.test_dialogs import closes_with_escape, comes_back_with, fits
from tests.e2e.test_phone_layout import overflow

# Materials no other journey uses, one per viewport, so the ledgers never meet.
RETURNED = {"phone": "CAB-H07VK-10-AZ", "desktop": "CAB-H07VK-10-MA"}


@pytest.mark.parametrize("viewport", ["phone", "desktop"])
def test_return_a_request_from_the_review_pages_dialog(browsing: Browsing, viewport: str) -> None:
    material = RETURNED[viewport]
    technician = browsing.as_("tech-mad", viewport)
    number = pages.submit_request(technician, "COL-MAD", {material: 5})
    engineer = browsing.as_("eng-mad", viewport)
    pages.open_in_inbox(engineer, number)
    # The page fits a phone, the facts are one block and the main action is in the sticky bar.
    assert overflow(engineer) <= 0
    expect(engineer.get_by_test_id("facts")).to_be_visible()
    bar = engineer.get_by_test_id("decide-bar")
    expect(bar).to_be_visible()
    expect(bar.get_by_test_id("approve-all")).to_be_visible()
    if viewport == "phone":
        box = bar.bounding_box()
        size = engineer.viewport_size
        assert box is not None and size is not None
        assert box["y"] + box["height"] <= size["height"]  # above the bottom edge, in reach

    engineer.locator("[popovertarget=request-menu]").click()
    engineer.get_by_test_id("return-open").click()
    dialog = engineer.get_by_test_id("return-dialog")
    expect(dialog).to_be_visible()
    fits(engineer, dialog, viewport)
    # A destructive dialog states what it does and its way out has the focus.
    expect(dialog.get_by_test_id("consequences")).to_contain_text("every decision")
    expect(dialog.get_by_test_id("dialog-cancel")).to_be_focused()
    engineer.keyboard.press("Escape")
    expect(dialog).to_be_hidden()

    engineer.locator("[popovertarget=request-menu]").click()
    engineer.get_by_test_id("return-open").click()
    dialog.get_by_test_id("return-submit").click()
    comes_back_with(dialog, "What should change")
    dialog.locator("[name=note]").fill("The quantity looks off")
    pages.send_dialog(engineer, dialog, "return-submit")
    expect(engineer).to_have_url(re.compile(r"/requests/inbox/"))
    expect(engineer.get_by_text("Request returned to its requester.")).to_be_visible()


@pytest.mark.parametrize("viewport", ["phone", "desktop"])
def test_a_roles_validity_in_a_lazy_dialog(browsing: Browsing, viewport: str) -> None:
    admin = browsing.as_("admin", viewport)
    admin.goto("/org/people/?q=mm-mad")
    admin.get_by_role("link", name="mm-mad@demo.example").click()
    assert overflow(admin) <= 0
    opener = admin.get_by_test_id("validity-open").first
    opener.click()
    dialog = admin.get_by_test_id("validity-dialog")
    expect(dialog.get_by_test_id("validity-form")).to_be_visible()
    fits(admin, dialog, viewport)
    closes_with_escape(admin, dialog, opener)

    opener.click()
    form = dialog.get_by_test_id("validity-form")
    form.locator("[name=valid_from]").fill("2030-01-01T08:00")
    form.locator("[name=valid_until]").fill("2026-01-01T08:00")
    dialog.get_by_test_id("validity-submit").click()
    comes_back_with(dialog, "The end must be after the start.")
    # Revoking is a confirmation that says what it does; leave it unsent.
    admin.keyboard.press("Escape")
    admin.get_by_test_id("revoke-open").first.click()
    revoke = admin.get_by_test_id("revoke-dialog")
    expect(revoke.get_by_test_id("consequences")).to_contain_text("loses what this role gives")
    expect(revoke.get_by_test_id("dialog-cancel")).to_be_focused()
    admin.keyboard.press("Escape")


def filter_chips_remove_one_filter(page: Page) -> None:
    chips = page.get_by_test_id("active-filters")
    expect(chips).to_contain_text("mm-")
    expect(chips).to_contain_text("Active")
    chips.get_by_role("link", name=re.compile("Search")).click()
    expect(page).to_have_url(re.compile(r"status=active"))
    expect(page).not_to_have_url(re.compile(r"q="))


@pytest.mark.parametrize("viewport", ["phone", "desktop"])
def test_list_filters_in_a_sheet_on_a_phone_and_as_chips(browsing: Browsing, viewport: str) -> None:
    admin = browsing.as_("admin", viewport)
    admin.goto("/org/people/")
    sheet = admin.locator("#list-filter-sheet")
    if viewport == "phone":
        expect(sheet).to_be_hidden()
        admin.get_by_test_id("filters-open").click()
        expect(sheet).to_be_visible()
        assert overflow(admin) <= 0
    else:
        expect(admin.get_by_test_id("filters-open")).to_be_hidden()
        expect(sheet).to_be_visible()  # the plain inline form
    admin.locator("#id_q").fill("mm-")
    admin.locator("#id_status").select_option(label="Active")
    expect(admin.get_by_test_id("active-filters")).to_contain_text("mm-")
    if viewport == "phone":
        sheet.get_by_role("button", name="Show the results").click()
        expect(sheet).to_be_hidden()
    filter_chips_remove_one_filter(admin)
