"""The administration screens of M10 (companies, branches, warehouses, units, the person's
dialogs) on a phone and a desktop: the pages with a record open, and each kind of dialog, fit the
screen, pass axe in both colour schemes and are reached and left with the keyboard alone. The
lists are in ``test_phone_layout`` (and so in ``test_accessibility``); the whole set-up flow is
``test_flow_administration``."""

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


@pytest.mark.parametrize(
    "path", ["/org/structure/companies/", "/org/warehouses/", "/catalog/units/"]
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
    for name in ("Companies", "Branches", "Warehouses", "Units of measure"):
        expect(group.get_by_role("link", name=name)).to_be_visible()
    technician = browsing.as_("tech-mad", "desktop")
    technician.goto("/requests/")
    expect(technician.locator("#app-menu [data-nav-group=administration]")).to_have_count(0)
    assert technician.goto("/org/structure/companies/").status == 403  # type: ignore[union-attr]
