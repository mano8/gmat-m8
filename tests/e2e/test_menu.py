"""The left menu keeps the chosen entry in view (M14.5, Q85, #68): every entry is a full page load,
so the menu's own scroll starts at 0 and the current entry can lie below its fold. The script
scrolls the menu, never the page, and writes nothing to the browser's storage."""

from playwright.sync_api import Page, ViewportSize, expect

from tests.e2e.conftest import Browsing

MENU = "#app-menu"
TALL_ENOUGH: ViewportSize = {"width": 1280, "height": 720}


def open_every_category(page: Page) -> None:
    """Unfold each category (remembered by the server, so the next page opens them too)."""
    for summary in page.locator(f"{MENU} details.nav-group:not([open]) > summary").all():
        with page.expect_response(lambda r: r.url.endswith("/ui/preferences/")):
            summary.click()


def inside(page: Page, container: str, entry: str) -> bool:
    return bool(
        page.evaluate(
            """([container, entry]) => {
                const c = document.querySelector(container).getBoundingClientRect();
                const e = document.querySelector(entry).getBoundingClientRect();
                return e.top >= c.top - 1 && e.bottom <= c.bottom + 1;
            }""",
            [container, entry],
        )
    )


def storage_is_untouched(page: Page) -> bool:
    """Nothing of ours in the browser's storage (htmx's own history bookmark is the one key the
    library writes by itself)."""
    return bool(
        page.evaluate(
            """() => localStorage.length === 0 && Object.keys(sessionStorage).every(
                (key) => key === "htmx-current-path-for-history")"""
        )
    )


def test_the_chosen_entry_is_in_view_in_a_long_menu(browsing: Browsing) -> None:
    page = browsing.as_("admin", "desktop")
    page.set_viewport_size(TALL_ENOUGH)
    page.goto("/")
    open_every_category(page)
    page.reload()
    menu = page.locator(MENU)
    assert page.evaluate(
        "(m) => { const e = document.querySelector(m); return e.scrollHeight > e.clientHeight; }",
        MENU,
    ), "the menu fits the window: nothing to prove"

    links = menu.locator("a.nav-link")
    hrefs = [str(links.nth(i).get_attribute("href")) for i in range(links.count())]
    for href in (hrefs[-1], hrefs[len(hrefs) // 2]):
        page.goto(href)
        current = f'{MENU} [aria-current="page"]'
        expect(page.locator(current)).to_have_count(1)
        assert inside(page, MENU, current), f"{href}: its entry is outside the menu's box"
        assert page.evaluate("window.scrollY") == 0
    assert storage_is_untouched(page)


def test_a_page_without_an_entry_leaves_the_menu_at_its_top(browsing: Browsing) -> None:
    page = browsing.as_("admin", "desktop")
    page.set_viewport_size(TALL_ENOUGH)
    page.goto("/account/sessions/")
    expect(page.locator(f'{MENU} [aria-current="page"]')).to_have_count(0)
    assert page.evaluate(f"document.querySelector('{MENU}').scrollTop") == 0
    assert storage_is_untouched(page)


def test_the_more_sheet_opens_on_the_current_entry(browsing: Browsing) -> None:
    page = browsing.as_("admin", "phone")
    page.goto("/audit/")
    page.get_by_role("button", name="More").click()
    sheet = page.locator("#more-menu")
    expect(sheet).to_be_visible()
    current = '#more-menu [aria-current="page"]'
    if page.locator(current).count():
        assert inside(page, "#more-menu", current)
    assert storage_is_untouched(page)
