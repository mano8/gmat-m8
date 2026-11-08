"""What the browser must never do or show (plan M8.2): a Content-Security-Policy violation fails
the test that caused it, and nothing of one person's session is left to the next person at the
same browser (``Cache-Control: no-store`` and htmx's empty history cache)."""

import pytest
from playwright.sync_api import expect

from tests.e2e import pages
from tests.e2e.conftest import Browsing
from tests.e2e.provision import sign_in


def test_the_csp_listener_catches_a_violation(browsing: Browsing) -> None:
    """The guard on every page works: an inline script the policy refuses is reported."""
    page = browsing.as_("tech-mad")
    page.goto("/")
    page.evaluate(
        "() => { const s = document.createElement('script');"
        " s.textContent = 'window.__inline = true'; document.head.append(s); }"
    )
    for _ in range(50):  # the report arrives through the context's binding
        if any(problem.startswith("csp:") for problem in browsing.problems):
            break
        page.wait_for_timeout(100)
    reported = [p for p in browsing.problems if p.startswith("csp:")]
    assert reported, "an inline script ran without a Content-Security-Policy violation"
    assert page.evaluate("window.__inline") is None
    browsing.problems.clear()  # expected here; every other test fails on one


@pytest.mark.parametrize("viewport", ["phone", "desktop"])
def test_back_after_logout_shows_nothing_of_the_first_person(
    browsing: Browsing, viewport: str
) -> None:
    first, second = browsing.people["tech-mad"], browsing.people["partner"]
    page = browsing.fresh_sign_in("tech-mad", viewport)
    page.goto("/requests/")
    expect(page.locator("main h1")).to_have_text("My requests")
    page.goto("/")
    expect(page.locator("main").get_by_text(first.email)).to_be_visible()

    pages.sign_out(page)
    page.wait_for_load_state("networkidle")
    expect(page.get_by_role("button", name="Account menu")).to_have_count(0)
    sign_in(page, second)

    mine = ("tech-mad@demo.example", "Tomás Herrero", "SOL-A-MAD")
    steps = 0
    while steps < 15:
        before = page.url
        if page.go_back() is None and page.url == before:
            break
        page.wait_for_load_state("networkidle")
        text = page.locator("body").inner_text()
        leaked = [word for word in mine if word in text]
        assert not leaked, f"Back showed {leaked} of the first person at {page.url}"
        steps += 1
    assert steps >= 3, "the test did not go back through the history"
    pages.forward(page)
