"""The app shell (M8b.2): the left menu of category dropdowns that can be hidden, the user menu,
the More sheet, the theme, and what the same forms do without JavaScript.

The preference cookie is read back through its effects (the page that follows), never through
``document.cookie``: it is ``HttpOnly``."""

import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e import pages
from tests.e2e.conftest import Browsing
from tests.e2e.test_phone_layout import overflow

MENU = "#app-menu"


def theme_of(page: Page) -> str | None:
    theme = page.evaluate("document.documentElement.getAttribute('data-theme')")
    return str(theme) if theme else None


def page_background(page: Page) -> str:
    return str(page.evaluate("getComputedStyle(document.body).backgroundColor"))


def toggle(page: Page, label: str) -> None:
    page.get_by_role("button", name=label).click()


def tab_reaches_the_menu(page: Page, presses: int = 40) -> bool:
    """Whether Tab, pressed from the top of the page, ever lands inside the left menu."""
    page.evaluate("document.activeElement && document.activeElement.blur()")
    page.locator("body").press("Home")
    for _ in range(presses):
        page.keyboard.press("Tab")
        if page.evaluate(f"!!(document.activeElement && document.activeElement.closest('{MENU}'))"):
            return True
    return False


def test_hide_the_menu_and_it_stays_hidden_on_the_next_page(browsing: Browsing) -> None:
    page = browsing.as_("wo-mad", "desktop")
    page.goto("/receiving/")
    expect(page.locator(MENU)).to_be_visible()
    assert tab_reaches_the_menu(page), "the shown menu is not in the tab order"

    toggle(page, "Hide the menu")
    expect(page.locator(MENU)).to_be_hidden()
    expect(page.get_by_role("button", name="Show the menu")).to_have_attribute(
        "aria-expanded", "false"
    )
    page.goto("/handovers/")  # the server renders the remembered state: no flash
    assert page.locator(MENU).get_attribute("hidden") is not None
    expect(page.locator(MENU)).to_be_hidden()
    assert not tab_reaches_the_menu(page), "a hidden menu's links can be reached with Tab"

    toggle(page, "Show the menu")
    expect(page.locator(MENU)).to_be_visible()
    page.reload()
    expect(page.locator(MENU)).to_be_visible()


def test_a_folded_category_stays_folded_and_the_current_one_stays_open(
    browsing: Browsing,
) -> None:
    page = browsing.as_("wo-mad", "desktop")
    page.goto("/receiving/")
    purchasing = page.locator(f"{MENU} [data-nav-group=purchasing]")
    site = page.locator(f"{MENU} [data-nav-group=site]")
    expect(purchasing).to_have_attribute("open", "")  # all open by default
    purchasing.locator("summary").click()
    expect(purchasing).not_to_have_attribute("open", "")
    page.wait_for_load_state("networkidle")
    page.reload()
    expect(purchasing).not_to_have_attribute("open", "")
    expect(site).to_have_attribute("open", "")
    # The category holding the page is open whatever was remembered.
    page.goto("/purchasing/orders/")
    expect(purchasing).to_have_attribute("open", "")
    # Put it back for the other tests of this person (the cookie is per context, but be tidy).
    page.goto("/receiving/")
    purchasing.locator("summary").click()
    expect(purchasing).to_have_attribute("open", "")


@pytest.mark.parametrize("viewport", ["phone", "desktop"])
def test_theme_system_dark_and_back_with_a_right_first_paint(
    browsing: Browsing, viewport: str
) -> None:
    page = browsing.as_("tech-mad", viewport)
    page.goto("/requests/")
    assert theme_of(page) is None
    light = page_background(page)

    pages.open_user_menu(page)
    toggle(page, "Dark")
    assert theme_of(page) == "dark"  # painted at once, no reload
    dark = page_background(page)
    assert dark != light
    page.wait_for_load_state("networkidle")
    page.reload()
    assert theme_of(page) == "dark"
    assert page_background(page) == dark  # the first paint after a reload is already dark
    page.goto("/projects/")
    assert theme_of(page) == "dark"

    pages.open_user_menu(page)
    expect(page.get_by_role("button", name="Dark")).to_have_attribute("aria-pressed", "true")
    toggle(page, "System")
    assert theme_of(page) is None
    page.wait_for_load_state("networkidle")
    page.reload()
    assert theme_of(page) is None
    assert page_background(page) == light


def test_the_login_page_has_the_theme_switch_for_a_signed_out_visitor(browsing: Browsing) -> None:
    page = browsing.anonymous("phone")
    page.goto("/accounts/login/")
    toggle(page, "Dark")
    assert theme_of(page) == "dark"
    page.wait_for_load_state("networkidle")
    page.reload()
    assert theme_of(page) == "dark"
    toggle(page, "System")
    assert theme_of(page) is None


@pytest.mark.parametrize("viewport", ["phone", "desktop"])
def test_the_user_menu_closes_with_escape_and_gives_focus_back(
    browsing: Browsing, viewport: str
) -> None:
    page = browsing.as_("tech-mad", viewport)
    page.goto("/")
    opener = page.get_by_role("button", name="Account menu")
    opener.click()
    expect(page.locator("#user-menu")).to_be_visible()
    for name in ("Sessions", "Notifications", "Security", "Sign out"):
        expect(page.locator("#user-menu").get_by_text(name, exact=True).first).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator("#user-menu")).to_be_hidden()
    expect(opener).to_be_focused()


def test_the_more_sheet_opens_under_the_bar_and_closes_with_escape(browsing: Browsing) -> None:
    page = browsing.as_("wo-mad", "phone")
    page.goto("/")
    opener = page.get_by_role("button", name="More")
    opener.click()
    sheet = page.locator("#more-menu")
    expect(sheet).to_be_visible()
    expect(sheet.get_by_role("link", name="Suppliers")).to_be_visible()
    assert overflow(page) <= 0
    # Every target is 44 px high at least.
    for link in sheet.get_by_role("link").all():
        box = link.bounding_box()
        assert box is not None and box["height"] >= 44
    page.keyboard.press("Escape")
    expect(sheet).to_be_hidden()
    expect(opener).to_be_focused()
    # Choosing an entry goes there.
    opener.click()
    sheet.get_by_role("link", name="Catalog").click()
    page.wait_for_url(re.compile(r"/catalog/$"))
    expect(page.locator("main h1")).to_be_visible()


def test_the_phone_has_no_menu_button_and_the_bar_holds_at_most_four_entries(
    browsing: Browsing,
) -> None:
    page = browsing.as_("tech-mad", "phone")
    page.goto("/requests/")
    expect(page.locator("[data-menu-toggle]")).to_be_hidden()
    expect(page.locator(MENU)).to_be_hidden()
    bar = page.get_by_role("navigation", name="Main navigation")
    assert bar.get_by_role("link").count() <= 4
    expect(bar.get_by_role("link", name="Requests")).to_have_attribute("aria-current", "page")


def test_the_menu_and_the_theme_work_without_javascript(browsing: Browsing) -> None:
    page = browsing.without_javascript("wo-mad", "desktop")
    page.goto("/receiving/")
    page.get_by_role("button", name="Hide the menu").click()  # a form: POST, then back here
    page.wait_for_url(re.compile(r"/receiving/$"))
    expect(page.locator(MENU)).to_be_hidden()
    page.get_by_role("button", name="Show the menu").click()
    expect(page.locator(MENU)).to_be_visible()

    page.get_by_role("button", name="Account menu").click()  # a popover: no script needed
    page.get_by_role("button", name="Dark").click()
    page.wait_for_url(re.compile(r"/receiving/$"))
    assert theme_of(page) == "dark"
    page.get_by_role("button", name="Account menu").click()
    page.get_by_role("button", name="System").click()
    assert theme_of(page) is None
    # The dropdowns still fold for the page.
    purchasing = page.locator(f"{MENU} [data-nav-group=purchasing]")
    purchasing.locator("summary").click()
    expect(purchasing).not_to_have_attribute("open", "")


def test_the_entrance_and_error_pages_fit_a_phone(browsing: Browsing) -> None:
    page = browsing.anonymous("phone")
    for path in ("/accounts/login/", "/accounts/password/reset/", "/no/such/page/"):
        page.goto(path)
        expect(page.locator("main h1").first).to_be_visible()
        assert overflow(page) <= 0, path
        assert page.get_by_role("navigation", name="Main navigation").count() == 0
