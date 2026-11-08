"""Screenshots of the main screens (M8b.6), for the person reviewing a change to the interface.

Opt-in: ``E2E_SCREENSHOTS=1`` saves each screen at 360 px and 1280 px, in the light and the dark
scheme, to ``test-results/screens/<screen>-<width>-<scheme>.png``. There is no pixel baseline to
compare with (fonts and platforms make that flaky): the pull request attaches the pictures and a
person looks at them.
"""

import os
from collections.abc import Callable

import pytest
from playwright.sync_api import Page, expect

from tests.e2e import pages
from tests.e2e.conftest import ROOT, Browsing
from tests.e2e.documents import PENDING_MATERIAL, Documents
from tests.e2e.test_accessibility import SCHEMES
from tests.e2e.test_home_charts import settled

pytestmark = pytest.mark.skipif(
    os.environ.get("E2E_SCREENSHOTS") != "1", reason="set E2E_SCREENSHOTS=1 to save screenshots"
)

FOLDER = ROOT / "test-results" / "screens"
WIDTHS = {"phone": 360, "desktop": 1280}


def nothing(page: Page, documents: Documents) -> None:
    """The page as it is."""


def line_dialog(page: Page, documents: Documents) -> None:
    pages.open_line_dialog(page, PENDING_MATERIAL)
    page.evaluate("document.querySelector('dialog[open]').getAnimations().map((a) => a.finish())")


Open = Callable[[Page, Documents], None]
SCREENS: dict[str, tuple[str, Callable[[Documents], str], Open]] = {
    "dashboard": ("admin", lambda d: "/", nothing),
    "dashboard-requester": ("tech-mad", lambda d: "/", nothing),
    "requests": ("tech-mad", lambda d: "/requests/", nothing),
    "request-status": ("tech-mad", lambda d: d.request_status, nothing),
    "review": ("eng-mad", lambda d: d.pending_request, nothing),
    "review-dialog": ("eng-mad", lambda d: d.pending_request, line_dialog),
    "order": ("mm-mad", lambda d: d.order, nothing),
    "receive-form": ("wo-mad", lambda d: d.receive, nothing),
    "people": ("admin", lambda d: "/org/people/", nothing),
}


@pytest.mark.parametrize("viewport", ["phone", "desktop"])
@pytest.mark.parametrize("name", sorted(SCREENS))
def test_save_the_screen(
    browsing: Browsing, documents: Documents, name: str, viewport: str
) -> None:
    person, url, prepare = SCREENS[name]
    FOLDER.mkdir(parents=True, exist_ok=True)
    page = browsing.as_(person, viewport)
    for scheme in SCHEMES:
        page.emulate_media(color_scheme=scheme, reduced_motion="reduce")
        page.goto(url(documents))
        expect(page.locator("main h1").first).to_be_visible()
        if url(documents) == "/":
            settled(page)  # the home page loads its panels and charts after it opens
        prepare(page, documents)
        page.wait_for_load_state("networkidle")
        page.screenshot(path=FOLDER / f"{name}-{WIDTHS[viewport]}-{scheme}.png", full_page=True)


def test_save_the_sign_in_page(browsing: Browsing) -> None:
    FOLDER.mkdir(parents=True, exist_ok=True)
    for viewport in ("phone", "desktop"):
        page = browsing.anonymous(viewport)
        for scheme in SCHEMES:
            page.emulate_media(color_scheme=scheme, reduced_motion="reduce")
            page.goto("/accounts/login/")
            expect(page.locator("main h1").first).to_be_visible()
            page.screenshot(
                path=FOLDER / f"sign-in-{WIDTHS[viewport]}-{scheme}.png", full_page=True
            )


def test_save_the_pictures_of_the_readme(browsing: Browsing, documents: Documents) -> None:
    """The two pictures of README.md: what the first screen of each looks like, as seen (not the
    whole page, whose fixed bars would show in the middle)."""
    FOLDER.mkdir(parents=True, exist_ok=True)
    desktop = browsing.as_("admin", "desktop")
    desktop.emulate_media(color_scheme="light", reduced_motion="reduce")
    desktop.goto("/")
    settled(desktop)
    desktop.wait_for_load_state("networkidle")
    desktop.screenshot(path=FOLDER / "readme-dashboard.png")
    phone = browsing.as_("tech-mad", "phone")
    phone.emulate_media(color_scheme="light", reduced_motion="reduce")
    phone.goto(documents.request_status)
    expect(phone.locator("main h1").first).to_be_visible()
    phone.screenshot(path=FOLDER / "readme-request-on-a-phone.png")
