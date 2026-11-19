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
from tests.e2e.test_accessibility import CHART_PAGES, SCHEMES
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


def finish_animations(page: Page) -> None:
    page.evaluate("document.querySelector('dialog[open]').getAnimations().map((a) => a.finish())")


def approve_all_dialog(page: Page, documents: Documents) -> None:
    page.get_by_test_id("approve-all-open").click()
    expect(page.get_by_test_id("approve-all-dialog")).to_be_visible()
    finish_animations(page)


def send_dialog(page: Page, documents: Documents) -> None:
    page.get_by_test_id("send-open").click()
    expect(page.get_by_test_id("send-dialog")).to_be_visible()
    finish_animations(page)


def follow(prefix: str) -> Callable[[Page, Documents], None]:
    """Open the first row of a list whose link starts with ``prefix``: the page it leads to is
    the screen (a company, a branch, a person)."""

    def prepare(page: Page, documents: Documents) -> None:
        page.locator(f"main a[href^='{prefix}']").first.click()
        page.wait_for_url(f"**{prefix}*/")
        expect(page.locator("main h1").first).to_be_visible()

    return prepare


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
    # M9.3: a project receiver's screens (the technician, through the project only).
    "site-project": ("tech-mad", lambda d: d.site_project, nothing),
    "site-expected": ("tech-mad", lambda d: d.site_expected, nothing),
    "site-receive-form": ("tech-mad", lambda d: d.site_receive, nothing),
    "site-receipt": ("tech-mad", lambda d: d.site_receipt, nothing),
    "people": ("admin", lambda d: "/org/people/", nothing),
    # M14: the request entry, approve all from the head, the administration overview, the send
    # dialog and the issue bar's notice; the left menu on a page far down (the menu, in view).
    "request-entry": ("tech-mad", lambda d: d.draft_request, nothing),
    "review-approve-all": ("eng-mad", lambda d: d.pending_request, approve_all_dialog),
    "administration": ("admin", lambda d: "/dashboard/administration/", nothing),
    "order-send-dialog": ("mm-mad", lambda d: d.order, send_dialog),
    "draft-order": ("mm-mad", lambda d: d.draft_order, nothing),
    "menu-current-entry": ("admin", lambda d: "/catalog/units/", nothing),
    # M16: pages whose buttons stopped filling their row (page actions beside the title, a form's
    # buttons side by side, a card's button as wide as its label).
    "project-page": ("admin", lambda d: d.site_project, nothing),
    "order-amendment": ("mm-mad", lambda d: f"{d.order}amendment/", nothing),
    "request-ledger": (
        "eng-mad",
        lambda d: d.pending_request.replace("examine/", "ledger/"),
        nothing,
    ),
    "catalog-new": ("admin", lambda d: "/catalog/new/", nothing),
    "exports": ("admin", lambda d: "/exports/", nothing),
    "handovers": ("wo-mad", lambda d: "/handovers/", nothing),
    "receive-orders": ("wo-mad", lambda d: "/receiving/orders/", nothing),
    "company": (
        "admin",
        lambda d: "/org/structure/companies/",
        follow("/org/structure/companies/"),
    ),
    "branch": ("admin", lambda d: "/org/structure/branches/", follow("/org/structure/branches/")),
    "person": ("admin", lambda d: "/org/people/", follow("/org/people/")),
}
WHOLE_PAGE = {
    nothing,
    *(
        spec[2]
        for spec in SCREENS.values()
        if spec[2] not in (line_dialog, approve_all_dialog, send_dialog)
    ),
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
        if url(documents) in CHART_PAGES:
            settled(page)  # the dashboards load their panels and charts after they open
        prepare(page, documents)
        page.wait_for_load_state("networkidle")
        path = FOLDER / f"{name}-{WIDTHS[viewport]}-{scheme}.png"
        if prepare in WHOLE_PAGE:
            pages.whole_page_screenshot(page, path)
        else:
            page.screenshot(path=path)  # a dialog: the screen it opens over


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
