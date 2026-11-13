"""Pages must fit a 360 px phone (CLAUDE.md: mobile-first templates): no page of the roles'
daily screens scrolls sideways. Data-heavy lists are visited with the rows the earlier flows
left behind."""

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import PHONE, Browsing

SCREENS = {
    "admin": [
        "/",
        "/org/people/",
        "/org/invitations/",
        "/audit/",
        "/audit/?per_page=100",
        "/exports/",
        "/catalog/",
        "/catalog/?per_page=100",
        "/projects/",
        "/suppliers/",
        "/purchasing/",
        "/purchasing/orders/",
        "/receiving/",
        "/handovers/",
        "/requests/",
        "/requests/inbox/",
        "/accounts/2fa/",
        "/account/notifications/",
        "/account/sessions/",
    ],
    "mm-mad": ["/", "/purchasing/", "/purchasing/orders/", "/requests/inbox/", "/catalog/"],
    "tech-mad": [
        "/",
        "/requests/",
        "/requests/new/",
        "/catalog/",
        "/projects/",
        "/receiving/",
        "/receiving/expected/",
        "/account/sessions/",
    ],
    "wo-mad": ["/", "/receiving/orders/", "/receiving/", "/handovers/", "/handovers/new/"],
    "site-manager": ["/", "/projects/", "/receiving/expected/", "/receiving/", "/handovers/"],
    "partner": [
        "/",
        "/requests/",
        "/requests/new/",
        "/projects/",
        "/receiving/expected/",
        "/receiving/",
    ],
}


def overflow(page: Page) -> int:
    """How many pixels wide the page's content is beyond the phone's width. (A mobile browser
    widens its own window to the content, so ``innerWidth`` cannot be the reference.)"""
    width = int(
        page.evaluate(
            "() => Math.max(document.documentElement.scrollWidth, document.body.scrollWidth)"
        )
    )
    return width - int(PHONE["viewport"]["width"])


@pytest.mark.parametrize("person", sorted(SCREENS))
def test_screens_fit_a_phone(browsing: Browsing, person: str) -> None:
    page = browsing.as_(person, "phone")
    wide = []
    for path in SCREENS[person]:
        page.goto(path)
        expect(page.locator("main h1").first).to_be_visible()
        expect(page.get_by_text("Loading…")).to_have_count(0)  # the dashboard's panels
        if (extra := overflow(page)) > 0:
            wide.append(f"{path}: {extra}px too wide")
    assert not wide, "\n".join(wide)
