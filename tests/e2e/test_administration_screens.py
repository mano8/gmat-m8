"""The administration screens of M10 (companies, branches, warehouses, units, the person's
dialogs) on a phone and a desktop: the pages with a record open, and each kind of dialog, fit the
screen, pass axe in both colour schemes and are reached and left with the keyboard alone. The
lists are in ``test_phone_layout`` (and so in ``test_accessibility``); the whole set-up flow is
``test_flow_administration``."""

import re
from collections.abc import Callable

import pytest
from playwright.sync_api import Locator, Page, expect

from tests.e2e.conftest import Browsing
from tests.e2e.test_accessibility import audit_schemes
from tests.e2e.test_dialogs import closes_with_escape, fits
from tests.e2e.test_keyboard import (
    assert_focus_is_visible,
    focus_by_keyboard,
    stays_in_the_dialog,
    walk_the_tab_order,
)
from tests.e2e.test_phone_layout import overflow

LIST_LINKS = {
    "company": ("/org/structure/companies/", "company-row"),
    "branch": ("/org/structure/branches/", "branch-row"),
}


def open_record(page: Page, kind: str) -> None:
    """The first record of a list, opened from its row."""
    path, row = LIST_LINKS[kind]
    page.goto(path)
    page.get_by_test_id(row).first.get_by_role("link").first.click()
    expect(page.get_by_test_id("page-header")).to_be_visible()


def open_new_dialog(page: Page) -> Locator:
    page.get_by_test_id("branch-new-open").first.click()
    dialog = page.get_by_test_id("branch-new-dialog")
    expect(dialog.get_by_test_id("branch-new-form")).to_be_visible()
    return dialog


def open_activity_dialog(page: Page) -> Locator:
    """A scope-defining change: the danger dialog with its preview, left unsent."""
    page.get_by_test_id("activity-open").first.click()
    dialog = page.get_by_test_id("activity-dialog")
    expect(dialog.get_by_test_id("preview")).to_be_visible()
    return dialog


def open_manager_dialog(page: Page) -> Locator:
    page.get_by_test_id("manager-open").click()
    dialog = page.locator("dialog[open]")
    expect(dialog.get_by_test_id("manager-form")).to_be_visible()
    return dialog


def open_nothing(page: Page) -> Locator:
    return page.locator("main")


DIALOGS: dict[str, tuple[str, Callable[[Page], Locator]]] = {
    "new branch (lazy)": ("company", open_new_dialog),
    "deactivate a company (lazy, danger, preview)": ("company", open_activity_dialog),
    "the managing branch (inline, danger)": ("branch", open_manager_dialog),
}


@pytest.mark.parametrize("viewport", ["phone", "desktop"])
@pytest.mark.parametrize("kind", sorted(LIST_LINKS))
def test_the_record_pages_fit_and_pass_axe(browsing: Browsing, kind: str, viewport: str) -> None:
    page = browsing.as_("admin", viewport)
    open_record(page, kind)
    if viewport == "phone":
        assert overflow(page) <= 0
    found = audit_schemes(page, f"{kind} page {viewport}")
    assert not found, "\n".join(found)


@pytest.mark.parametrize("viewport", ["phone", "desktop"])
@pytest.mark.parametrize("name", sorted(DIALOGS))
def test_the_dialogs_fit_pass_axe_and_close_with_the_keyboard(
    browsing: Browsing, name: str, viewport: str
) -> None:
    kind, opener = DIALOGS[name]
    page = browsing.as_("admin", viewport)
    open_record(page, kind)
    dialog = opener(page)
    fits(page, dialog, viewport)
    found = audit_schemes(page, f"{name} {viewport}")
    assert not found, "\n".join(found)
    stays_in_the_dialog(page, "dialog", presses=8)
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()


@pytest.mark.parametrize("viewport", ["phone", "desktop"])
def test_a_lazy_dialog_is_opened_and_left_by_keyboard(browsing: Browsing, viewport: str) -> None:
    page = browsing.as_("admin", viewport)
    page.goto("/catalog/units/")
    opener = page.get_by_test_id("unit-new-open").first
    focus_by_keyboard(page, opener)
    assert_focus_is_visible(page)
    page.keyboard.press("Enter")
    dialog = page.get_by_test_id("unit-new-dialog")
    expect(dialog.get_by_test_id("unit-new-form")).to_be_visible()
    fits(page, dialog, viewport)
    found = audit_schemes(page, f"new unit dialog {viewport}")
    assert not found, "\n".join(found)
    stays_in_the_dialog(page, "dialog")
    closes_with_escape(page, dialog, opener)
    assert_focus_is_visible(page)


@pytest.mark.parametrize("viewport", ["phone", "desktop"])
def test_a_unit_in_use_says_so_and_stays_active(browsing: Browsing, viewport: str) -> None:
    """The demo's materials use ``UD``: the confirmation names them, passes axe and keeps the
    focus; confirming anyway brings the refusal into the dialog, and the unit stays."""
    page = browsing.as_("admin", viewport)
    page.goto("/catalog/units/?q=UD&state=active")
    row = page.get_by_test_id("unit-row").filter(
        has=page.locator(".doc-no", has_text=re.compile(r"^UD$"))
    )
    row.get_by_test_id("unit-deactivate-open").click()
    dialog = page.get_by_test_id("unit-activity-dialog")
    expect(dialog.get_by_test_id("unit-in-use")).to_be_visible()
    fits(page, dialog, viewport)
    found = audit_schemes(page, f"deactivate a unit in use {viewport}")
    assert not found, "\n".join(found)
    stays_in_the_dialog(page, "dialog")
    dialog.get_by_test_id("unit-deactivate-submit").click()
    expect(dialog.locator("[data-error-summary]")).to_be_visible()
    expect(dialog).to_be_visible()
    page.goto("/catalog/units/?q=UD&state=inactive")
    expect(page.locator(".doc-no", has_text=re.compile(r"^UD$"))).to_have_count(0)


@pytest.mark.parametrize(
    "path",
    [
        "/dashboard/administration/",
        "/org/notifications/",
        "/org/structure/companies/",
        "/org/warehouses/",
        "/catalog/units/",
    ],
)
def test_the_tab_order_of_the_lists(browsing: Browsing, path: str) -> None:
    page = browsing.as_("admin", "desktop")
    page.goto(path)
    expect(page.locator("main h1").first).to_be_visible()
    stops = walk_the_tab_order(page)
    assert any(s["region"] == "main" for s in stops)


def test_the_administration_menu_is_the_group_administrators_only(browsing: Browsing) -> None:
    admin = browsing.as_("admin", "desktop")
    admin.goto("/")
    group = admin.locator("#app-menu [data-nav-group=administration]")
    expect(group).to_be_visible()
    for name in (
        "Overview",
        "People",
        "Notifications",
        "Companies",
        "Branches",
        "Warehouses",
        "Units of measure",
    ):
        expect(group.get_by_role("link", name=name)).to_be_visible()
    technician = browsing.as_("tech-mad", "desktop")
    technician.goto("/requests/")
    expect(technician.locator("#app-menu [data-nav-group=administration]")).to_have_count(0)
    response = technician.goto("/org/structure/companies/")
    assert response is not None and response.status == 403


# --- the notification screens (M15.3, M17.4) -------------------------------------------------


@pytest.mark.parametrize("viewport", ["phone", "desktop"])
def test_the_notifications_page_and_its_dialog(browsing: Browsing, viewport: str) -> None:
    """Every notification with its state on each channel; the dialog of one fits the screen,
    passes axe, keeps the keyboard inside and closes with Escape; the page itself fits a phone and
    passes axe."""
    page = browsing.as_("admin", viewport)
    page.goto("/org/notifications/")
    rows = page.get_by_test_id("notice-row")
    expect(rows).to_have_count(6)
    expect(rows.first.get_by_test_id("notice-state")).to_have_count(2)  # email, in the application
    if viewport == "phone":
        assert overflow(page) <= 0
    found = audit_schemes(page, f"notifications {viewport}")
    assert not found, "\n".join(found)
    opener = page.get_by_test_id("notice-request-to-review-open")
    focus_by_keyboard(page, opener)
    assert_focus_is_visible(page)
    page.keyboard.press("Enter")
    dialog = page.get_by_test_id("notice-request-to-review-dialog")
    expect(dialog.get_by_test_id("notice-request-to-review-form")).to_be_visible()
    expect(dialog.get_by_test_id("consequences")).to_be_visible()
    expect(dialog.get_by_role("combobox")).to_have_count(2)  # one value per channel
    fits(page, dialog, viewport)
    found = audit_schemes(page, f"notification dialog {viewport}")
    assert not found, "\n".join(found)
    stays_in_the_dialog(page, "dialog", presses=8)
    closes_with_escape(page, dialog, opener)
    assert_focus_is_visible(page)


@pytest.mark.parametrize("viewport", ["phone", "desktop"])
@pytest.mark.parametrize("channel", ["email", "in-app"])
def test_a_persons_notifications_dialog(browsing: Browsing, viewport: str, channel: str) -> None:
    """The person's page lists every notification with its state on each channel; the dialog of
    one channel fits, passes axe and is left by keyboard, nothing saved."""
    page = browsing.as_("admin", viewport)
    page.goto("/org/people/?q=tech-mad")
    page.get_by_role("link", name="tech-mad@demo.example").click()
    section = page.get_by_test_id("person-notices")
    expect(section.get_by_test_id("notice-state")).to_have_count(6)
    if viewport == "phone":
        assert overflow(page) <= 0
    found = audit_schemes(page, f"person notifications {viewport}")
    assert not found, "\n".join(found)
    opener = page.get_by_test_id(f"notices-{channel}-open")
    focus_by_keyboard(page, opener)
    page.keyboard.press("Enter")
    dialog = page.get_by_test_id(f"notices-{channel}-dialog")
    expect(dialog.get_by_test_id(f"notices-{channel}-form")).to_be_visible()
    expect(dialog.get_by_role("combobox")).to_have_count(6)
    fits(page, dialog, viewport)
    found = audit_schemes(page, f"person notifications dialog {channel} {viewport}")
    assert not found, "\n".join(found)
    stays_in_the_dialog(page, "dialog", presses=10)
    closes_with_escape(page, dialog, opener)


@pytest.mark.parametrize("viewport", ["phone", "desktop"])
def test_my_notifications_and_each_of_its_dialogs(browsing: Browsing, viewport: str) -> None:
    """*My notifications*: where, then what; each dialog fits the screen, passes axe, keeps the
    keyboard inside and closes with Escape, nothing saved. At 360 px the table is one card per
    notification, so the page does not scroll sideways."""
    page = browsing.as_("tech-mad", viewport)
    page.goto("/account/notifications/")
    expect(page.get_by_test_id("channel")).to_have_count(2)
    expect(page.get_by_test_id("notice-state")).to_have_count(6)
    if viewport == "phone":
        assert overflow(page) <= 0
    found = audit_schemes(page, f"my notifications {viewport}")
    assert not found, "\n".join(found)
    for opener_id, dialog_id, form_id, selects in (
        ("channels-open", "channels-dialog", "channels-form", 0),
        ("choices-email-open", "choices-email-dialog", "choices-email-form", 6),
        ("choices-in-app-open", "choices-in-app-dialog", "choices-in-app-form", 6),
    ):
        opener = page.get_by_test_id(opener_id)
        focus_by_keyboard(page, opener)
        assert_focus_is_visible(page)
        page.keyboard.press("Enter")
        dialog = page.get_by_test_id(dialog_id)
        expect(dialog.get_by_test_id(form_id)).to_be_visible()
        if selects:
            expect(dialog.get_by_role("combobox")).to_have_count(selects)
        else:
            expect(dialog.get_by_role("checkbox")).to_have_count(2)
        fits(page, dialog, viewport)
        found = audit_schemes(page, f"my notifications dialog {dialog_id} {viewport}")
        assert not found, "\n".join(found)
        stays_in_the_dialog(page, "dialog", presses=10)
        closes_with_escape(page, dialog, opener)
        assert_focus_is_visible(page)


@pytest.mark.parametrize("viewport", ["phone", "desktop"])
def test_the_bell_and_the_list_of_notifications(browsing: Browsing, viewport: str) -> None:
    """The bell is in every page's header, a link to the list with the count in its name; the
    list (empty or not) fits a phone and passes axe."""
    page = browsing.as_("tech-mad", viewport)
    page.goto("/requests/")
    bell = page.get_by_test_id("notification-bell")
    expect(bell).to_be_visible()
    expect(bell).to_have_attribute("aria-label", re.compile(r"^Notifications"))
    bell.click()
    page.wait_for_url("**/notifications/")
    expect(page.locator("main h1").first).to_have_text("Notifications")
    if viewport == "phone":
        assert overflow(page) <= 0
    found = audit_schemes(page, f"notifications list {viewport}")
    assert not found, "\n".join(found)
