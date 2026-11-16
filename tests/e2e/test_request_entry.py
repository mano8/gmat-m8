"""Request entry by keyboard (plan M14.1): Enter in a quantity saves it and moves the focus on to
the next quantity, the last one to *Review and submit*; Tab keeps its place; a refused quantity
keeps the focus on its field. Once on a phone and once on a desktop."""

import pytest
from playwright.sync_api import Page, expect

from tests.e2e import pages
from tests.e2e.conftest import Browsing

REFERENCES = ("CAB-H07VK-2,5-AZ", "CAB-H07VK-2,5-GR", "CAB-H07VK-2,5-MA")


def _draft_with_lines(page: Page) -> list[str]:
    pages.start_draft(page, "HOSP-X")
    for reference in REFERENCES:
        page.fill("#id_q", reference)
        page.click(f"button[data-material='{reference}']")
        expect(pages.line_of(page, reference)).to_be_visible()
    return [
        pages.line_of(page, reference).locator("input[name=requested_qty]").get_attribute("id")
        or ""
        for reference in REFERENCES
    ]


def _enter(page: Page, field_id: str, value: str) -> None:
    with page.expect_response(lambda r: "/update/" in r.url and r.request.method == "POST"):
        page.fill(f"#{field_id}", value)
        page.press(f"#{field_id}", "Enter")


@pytest.mark.parametrize("viewport", ["phone", "desktop"])
def test_enter_moves_on_to_the_next_quantity(browsing: Browsing, viewport: str) -> None:
    page = browsing.as_("tech-mad", viewport)
    first, second, third = _draft_with_lines(page)
    assert all(field.startswith("qty-") for field in (first, second, third))

    _enter(page, first, "3")
    expect(page.locator(f"#{second}")).to_be_focused()
    page.keyboard.type("4")  # the field's text was selected: typing replaces it
    page.keyboard.press("Enter")
    expect(page.locator(f"#{third}")).to_be_focused()
    page.keyboard.type("5")
    page.keyboard.press("Enter")
    expect(page.locator("#review-link")).to_be_focused()
    for field, value in ((first, "3"), (second, "4"), (third, "5")):
        expect(page.locator(f"#{field}")).to_have_value(value)


@pytest.mark.parametrize("viewport", ["phone", "desktop"])
def test_tab_and_a_refused_quantity_keep_the_focus(browsing: Browsing, viewport: str) -> None:
    page = browsing.as_("tech-mad", viewport)
    first, second, _ = _draft_with_lines(page)

    page.fill(f"#{first}", "7")
    with page.expect_response(lambda r: "/update/" in r.url and r.request.method == "POST"):
        page.press(f"#{first}", "Tab")
    expect(page.locator(f"#{first}")).to_have_value("7")
    assert page.evaluate("document.activeElement.id") != "", "the focus fell back to the page"

    _enter(page, second, "0")
    expect(page.get_by_test_id("lines-error")).to_be_visible()
    expect(page.locator(f"#{second}")).to_be_focused()


@pytest.mark.parametrize("viewport", ["phone", "desktop"])
def test_removing_a_line_keeps_the_focus_in_the_lines(browsing: Browsing, viewport: str) -> None:
    page = browsing.as_("tech-mad", viewport)
    first, second, _ = _draft_with_lines(page)
    remove = first.replace("qty-", "remove-")
    with page.expect_response(lambda r: "/remove/" in r.url and r.request.method == "POST"):
        page.press(f"#{remove}", "Enter")
    expect(page.locator(f"#{second}")).to_be_focused()
