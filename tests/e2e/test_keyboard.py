"""Keyboard walk (M8b.6): the shell's tab order, each kind of dialog, the menus and the More sheet,
all reached and left with the keyboard alone, with a visible focus at every stop.

"Visible" is read from the computed style of the focused element (an outline with a width) and
from its box (it has a size and is inside the viewport). The order is the document's: no positive
``tabindex`` anywhere, and every stop comes after the previous one in the document.
"""

import pytest
from playwright.sync_api import Locator, Page, expect

from tests.e2e.conftest import Browsing
from tests.e2e.documents import PENDING_MATERIAL, Documents

FOCUS = """() => {
  const el = document.activeElement;
  if (!el || el === document.body || el === document.documentElement) return null;
  const style = getComputedStyle(el);
  const box = el.getBoundingClientRect();
  const region = (el.closest('main') && 'main') || (el.closest('nav') && 'nav')
    || (el.closest('header') && 'header') || (el.closest('dialog') && 'dialog')
    || (el.closest('footer') && 'footer') || 'other';
  return {
    name: el.tagName.toLowerCase() + (el.name ? '#' + el.name : '')
      + (el.dataset.testid ? '[' + el.dataset.testid + ']' : '')
      + ' ' + (el.getAttribute('aria-label') || el.textContent || '').trim().slice(0, 40),
    // The picker button inside a date field draws its own ring: the field itself is not
    // :focus-visible then.
    outline: (style.outlineStyle !== 'none' && parseFloat(style.outlineWidth) > 0)
      || (el.matches('input[type=date], input[type=datetime-local], input[type=time]')
        && !el.matches(':focus-visible')),
    sized: box.width > 0 && box.height > 0,
    inView: box.bottom > 0 && box.right > 0
      && box.top < window.innerHeight && box.left < window.innerWidth,
    region,
  };
}"""
POSITIVE_TABINDEX = "[tabindex]:not([tabindex='0']):not([tabindex='-1'])"
Stop = dict[str, object]


def stop(page: Page) -> Stop | None:
    found = page.evaluate(FOCUS)
    return found if isinstance(found, dict) else None


def assert_focus_is_visible(page: Page) -> Stop:
    found = stop(page)
    assert found is not None, "the focus is on nothing"
    assert found["sized"] and found["inView"], f"the focus is out of sight: {found['name']}"
    assert found["outline"], f"the focused element shows no outline: {found['name']}"
    return found


def inside(page: Page, selector: str) -> bool:
    return bool(
        page.evaluate(
            "(selector) => !!(document.activeElement && document.activeElement.closest(selector))",
            selector,
        )
    )


def focus_by_keyboard(page: Page, target: Locator) -> None:
    """Put the focus on ``target`` the way a keyboard does (the browser then draws the
    focus-visible ring), after one key press so the last input is the keyboard."""
    page.keyboard.press("Tab")
    target.focus()


def walk_the_tab_order(page: Page, limit: int = 80) -> list[Stop]:
    """Tab from the top of the page until the focus leaves the document or ``limit`` stops."""
    assert page.locator(POSITIVE_TABINDEX).count() == 0, "a positive tabindex reorders the page"
    page.evaluate("document.activeElement && document.activeElement.blur()")
    page.evaluate("window.scrollTo(0, 0)")
    stops: list[Stop] = []
    page.evaluate("window.__previousStop = null")
    for _ in range(limit):
        page.keyboard.press("Tab")
        found = assert_focus_is_visible(page) if stop(page) else None
        if found is None:
            break
        assert page.evaluate(
            # (A date field takes several stops of its own: the same element again is fine.)
            "() => !window.__previousStop || window.__previousStop === document.activeElement"
            " || !!(window.__previousStop.compareDocumentPosition(document.activeElement)"
            " & Node.DOCUMENT_POSITION_FOLLOWING)"
        ), f"the focus went back up the page to {found['name']}, after {[x['name'] for x in stops]}"
        page.evaluate("window.__previousStop = document.activeElement")
        stops.append(found)
    return stops


@pytest.mark.parametrize("viewport", ["phone", "desktop"])
@pytest.mark.parametrize("person", ["wo-mad", "tech-mad"])
def test_the_tab_order_of_the_shell(browsing: Browsing, person: str, viewport: str) -> None:
    page = browsing.as_(person, viewport)
    page.goto("/receiving/" if person == "wo-mad" else "/requests/")
    expect(page.locator("main h1").first).to_be_visible()
    stops = walk_the_tab_order(page)
    assert len(stops) >= 5
    # The first stop is the skip link, and it is on screen when it has the focus.
    assert str(stops[0]["name"]).endswith("Skip to content"), stops[0]
    regions = [str(s["region"]) for s in stops[1:]]
    assert "main" in regions
    # Whatever the viewport draws first (the top bar or the menu), the content comes after the
    # banner and the side menu, in the document's order.
    assert regions.index("main") >= 1
    # The skip link takes the focus to the content.
    page.evaluate("document.activeElement && document.activeElement.blur()")
    page.keyboard.press("Tab")
    page.keyboard.press("Enter")
    page.keyboard.press("Tab")
    assert inside(page, "main"), "after the skip link, the next stop is not in the content"


@pytest.mark.parametrize("viewport", ["phone", "desktop"])
def test_each_kind_of_dialog_by_keyboard(
    browsing: Browsing, documents: Documents, viewport: str
) -> None:
    page = browsing.as_("eng-mad", viewport)
    page.goto(documents.pending_request)
    expect(page.locator("main h1").first).to_be_visible()

    # A lazy dialog: its form is loaded into it when it opens.
    opener = (
        page.get_by_test_id("review-line")
        .filter(has_text=PENDING_MATERIAL)
        .get_by_test_id("edit-line")
    )
    focus_by_keyboard(page, opener)
    assert_focus_is_visible(page)
    page.keyboard.press("Enter")
    dialog = page.get_by_test_id("line-dialog")
    expect(dialog.get_by_test_id("line-form")).to_be_visible()
    stays_in_the_dialog(page, "dialog")
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    expect(opener).to_be_focused()
    assert_focus_is_visible(page)

    # A destructive confirmation, opened from a menu item: the way out has the focus.
    menu_button = page.locator("[popovertarget=request-menu]")
    focus_by_keyboard(page, menu_button)
    page.keyboard.press("Enter")
    expect(page.locator("#request-menu")).to_be_visible()
    page.keyboard.press("Tab")
    assert inside(page, "#request-menu"), "the menu's first item is not the next stop"
    assert_focus_is_visible(page)
    page.get_by_test_id("return-open").focus()
    page.keyboard.press("Enter")
    confirm = page.get_by_test_id("return-dialog")
    expect(confirm).to_be_visible()
    expect(confirm.get_by_test_id("dialog-cancel")).to_be_focused()
    assert_focus_is_visible(page)
    stays_in_the_dialog(page, "dialog")
    page.keyboard.press("Escape")
    expect(confirm).to_be_hidden()
    expect(menu_button).to_be_focused()


def stays_in_the_dialog(page: Page, selector: str, presses: int = 14) -> None:
    """Tab and Shift+Tab never reach the page behind a modal dialog (the focus wraps inside it,
    or to the browser's own controls, where the document has none)."""
    for key in ["Tab"] * presses + ["Shift+Tab"] * presses:
        page.keyboard.press(key)
        found = stop(page)
        if found is None:
            continue
        assert inside(page, selector), f"the focus left the dialog for {found['name']}"
        assert_focus_is_visible(page)


def test_the_filter_sheet_by_keyboard_on_a_phone(browsing: Browsing) -> None:
    page = browsing.as_("admin", "phone")
    page.goto("/org/people/")
    opener = page.get_by_test_id("filters-open")
    sheet = page.locator("#list-filter-sheet")
    focus_by_keyboard(page, opener)
    assert_focus_is_visible(page)
    page.keyboard.press("Enter")
    expect(sheet).to_be_visible()
    page.keyboard.press("Tab")
    assert inside(page, "#list-filter-sheet"), "Tab from Filters does not enter the sheet"
    assert_focus_is_visible(page)
    page.keyboard.press("Escape")
    expect(sheet).to_be_hidden()
    expect(opener).to_be_focused()

    # The sheet is not modal: when the keyboard leaves it, it closes rather than stay open
    # over the page the focus went to.
    page.keyboard.press("Enter")
    expect(sheet).to_be_visible()
    for _ in range(20):
        page.keyboard.press("Tab")
        if not inside(page, "#list-filter-sheet"):
            break
    expect(sheet).to_be_hidden()
    assert_focus_is_visible(page)


@pytest.mark.parametrize("viewport", ["phone", "desktop"])
def test_the_account_menu_by_keyboard(browsing: Browsing, viewport: str) -> None:
    page = browsing.as_("tech-mad", viewport)
    page.goto("/requests/")
    opener = page.get_by_role("button", name="Account menu")
    focus_by_keyboard(page, opener)
    assert_focus_is_visible(page)
    page.keyboard.press("Enter")
    expect(page.locator("#user-menu")).to_be_visible()
    page.keyboard.press("Tab")
    assert inside(page, "#user-menu"), "Tab from the button does not enter the open menu"
    assert_focus_is_visible(page)
    page.keyboard.press("Escape")
    expect(page.locator("#user-menu")).to_be_hidden()
    expect(opener).to_be_focused()
    assert_focus_is_visible(page)


def test_the_more_sheet_by_keyboard(browsing: Browsing) -> None:
    page = browsing.as_("wo-mad", "phone")
    page.goto("/")
    opener = page.get_by_role("button", name="More")
    focus_by_keyboard(page, opener)
    assert_focus_is_visible(page)
    page.keyboard.press("Enter")
    sheet = page.locator("#more-menu")
    expect(sheet).to_be_visible()
    page.keyboard.press("Tab")
    assert inside(page, "#more-menu"), "Tab from More does not enter the sheet"
    assert_focus_is_visible(page)
    page.keyboard.press("Escape")
    expect(sheet).to_be_hidden()
    expect(opener).to_be_focused()


def test_the_menu_categories_open_and_close_with_the_keyboard(browsing: Browsing) -> None:
    page = browsing.as_("wo-mad", "desktop")
    page.goto("/receiving/")
    group = page.locator("#app-menu [data-nav-group=purchasing]")
    summary = group.locator("summary")
    focus_by_keyboard(page, summary)
    assert_focus_is_visible(page)
    expect(group).to_have_attribute("open", "")
    with page.expect_response(lambda r: r.url.endswith("/ui/preferences/")):
        page.keyboard.press("Enter")
    expect(group).not_to_have_attribute("open", "")
    assert_focus_is_visible(page)
    with page.expect_response(lambda r: r.url.endswith("/ui/preferences/")):
        page.keyboard.press("Space")
    expect(group).to_have_attribute("open", "")
    assert_focus_is_visible(page)
