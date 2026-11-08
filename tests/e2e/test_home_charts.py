"""The home page's key figures and charts (M8b.5) for every demo role, on a phone and a desktop:
every panel loads, every chart that has data is drawn (its canvas shown, its table folded), the
keyboard reaches a chart's values, and nothing scrolls sideways. The ``browsing`` fixture fails
the test on a CSP violation, an uncaught script error or a 5xx, so Chart.js and charts.js are
proved to run under the application's CSP.

The page is read with the documents the earlier flows of the suite left behind; a chart without
data shows its empty message instead of a canvas, which is checked the same way."""

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import Browsing
from tests.e2e.test_phone_layout import overflow

LIGHT = "() => !window.matchMedia('(prefers-color-scheme: dark)').matches"
PEOPLE = ("admin", "mm-mad", "eng-mad", "wo-mad", "tech-mad", "site-manager", "partner")


def settled(page: Page) -> None:
    """Every lazily loaded part of the home page has replaced its placeholder."""
    expect(page.locator("main [aria-busy='true']")).to_have_count(0, timeout=15_000)


def drawn_charts(page: Page) -> int:
    cards = page.locator(".chart-card:has(canvas[data-chart])")
    count = cards.count()
    for index in range(count):
        card = cards.nth(index)
        canvas = card.locator("canvas[data-chart]")
        expect(canvas).to_be_visible()
        expect(canvas).to_have_attribute("role", "img")
        expect(card.locator("details[data-chart-table]")).not_to_have_attribute("open", "")
    assert page.evaluate(
        "() => [...document.querySelectorAll('canvas[data-chart]')]"
        ".every((c) => window.Chart && window.Chart.getChart(c))"
    ), "a canvas without its chart"
    return count


@pytest.mark.parametrize("viewport", ["phone", "desktop"])
@pytest.mark.parametrize("person", PEOPLE)
def test_the_home_page_of_each_role(browsing: Browsing, person: str, viewport: str) -> None:
    page = browsing.as_(person, viewport)
    page.goto("/")
    settled(page)
    drawn_charts(page)
    if viewport == "phone":
        assert overflow(page) <= 0, f"{person}: the home page scrolls sideways"


def test_the_period_and_the_keyboard(browsing: Browsing) -> None:
    page = browsing.as_("admin", "desktop")
    page.goto("/?weeks=4")
    settled(page)
    period = page.get_by_role("navigation", name="Period")
    expect(period.get_by_role("link", name="4 weeks")).to_have_attribute("aria-current", "true")
    if not drawn_charts(page):
        pytest.skip("no chart has data on this stack")
    canvas = page.locator("canvas[data-chart]").first
    canvas.focus()
    canvas.press("ArrowRight")
    shown = "(c) => window.Chart.getChart(c).tooltip.getActiveElements().length"
    assert canvas.evaluate(shown) > 0, "the keyboard does not reach the chart's values"
    canvas.press("Escape")
    assert canvas.evaluate(shown) == 0
    # A change of colour scheme redraws every chart in the other palette (the theme switch
    # sends gmat:theme, which does the same).
    before = canvas.evaluate("(c) => window.Chart.getChart(c).id")
    page.emulate_media(color_scheme="dark" if page.evaluate(LIGHT) else "light")
    page.wait_for_function(
        "(id) => window.Chart.getChart(document.querySelector('canvas[data-chart]')).id !== id",
        arg=before,
    )
    assert drawn_charts(page)
