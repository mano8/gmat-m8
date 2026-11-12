"""Automated accessibility (M8b.6): axe-core on the screens people use, in both colour schemes, on
a phone (360 px) and a desktop.

axe is injected with ``page.evaluate`` (Playwright's own channel, which the Content-Security-Policy
does not govern), so the policy stays as it is; the CSP listener of ``browsing`` still fails a test
if the injection or the run trips it. The gate is **no serious or critical violation**; every
other finding is written to ``test-results/axe-report.txt`` (the pull request lists them).
"""

import re
from collections.abc import Callable, Iterator
from typing import Any, Literal

import pytest
from axe_playwright_python.sync_playwright import Axe
from playwright.sync_api import Page, expect

from tests.e2e import pages
from tests.e2e.conftest import ROOT, Browsing
from tests.e2e.documents import PENDING_MATERIAL, Documents
from tests.e2e.test_home_charts import settled
from tests.e2e.test_phone_layout import SCREENS

BLOCKING = {"serious", "critical"}
SCHEMES: tuple[Literal["light", "dark"], ...] = ("light", "dark")
# The WCAG 2.2 AA rules and the best practices (those are only listed); experimental rules are off.
OPTIONS = {
    "resultTypes": ["violations"],
    "runOnly": {
        "type": "tag",
        "values": ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"],
    },
}
AUDITED = {**SCREENS, "eng-mad": ["/", "/requests/inbox/", "/purchasing/orders/"]}

REPORT: list[str] = []


@pytest.fixture(scope="module", autouse=True)
def write_report() -> Iterator[None]:
    yield
    folder = ROOT / "test-results"
    folder.mkdir(exist_ok=True)
    (folder / "axe-report.txt").write_text(
        "\n".join(sorted(set(REPORT))) + "\n" if REPORT else "No finding below serious.\n",
        encoding="utf-8",
    )


def describe(label: str, violation: dict[str, Any]) -> str:
    nodes = violation["nodes"]
    where = "; ".join(" ".join(str(t) for t in node["target"]) for node in nodes[:3])
    return (
        f"[{violation['impact']}] {violation['id']} on {label}: {violation['help']} "
        f"({len(nodes)} element(s): {where})"
    )


def audit(page: Page, label: str) -> list[str]:
    """Run axe once; returns the blocking findings, records the rest."""
    violations = Axe().run(page, options=OPTIONS).response["violations"]
    blocking = []
    for violation in violations:
        line = describe(label, violation)
        if violation["impact"] in BLOCKING:
            blocking.append(line)
        else:
            REPORT.append(line)
    return blocking


def audit_schemes(page: Page, label: str, *, charts: bool = False) -> list[str]:
    found = []
    for scheme in SCHEMES:
        # No motion: a colour caught in the middle of a transition is not what people see.
        page.emulate_media(color_scheme=scheme, reduced_motion="reduce")
        page.evaluate(
            "Promise.all(document.getAnimations().map((a) => a.finished.catch(() => null)))"
        )
        if charts:
            settled(page)
        found += audit(page, f"{label} ({scheme})")
    return found


@pytest.mark.parametrize("viewport", ["phone", "desktop"])
def test_the_entrance_pages(browsing: Browsing, viewport: str) -> None:
    page = browsing.anonymous(viewport)
    found = []
    for path in ("/accounts/login/", "/accounts/password/reset/", "/no/such/page/"):
        page.goto(path)
        expect(page.locator("main h1").first).to_be_visible()
        found += audit_schemes(page, f"{path} {viewport}")
    assert not found, "\n".join(found)


@pytest.mark.parametrize("viewport", ["phone", "desktop"])
@pytest.mark.parametrize("person", sorted(AUDITED))
def test_the_home_pages_and_the_lists(browsing: Browsing, person: str, viewport: str) -> None:
    page = browsing.as_(person, viewport)
    found = []
    for path in AUDITED[person]:
        page.goto(path)
        expect(page.locator("main h1").first).to_be_visible()
        found += audit_schemes(page, f"{person} {path} {viewport}", charts=path == "/")
    assert not found, "\n".join(found)


def open_a_dialog(page: Page, documents: Documents) -> None:
    pages.open_line_dialog(page, PENDING_MATERIAL)


def open_the_return_dialog(page: Page, documents: Documents) -> None:
    page.locator("[popovertarget=request-menu]").click()
    page.get_by_test_id("return-open").click()
    expect(page.get_by_test_id("return-dialog")).to_be_visible()


def nothing(page: Page, documents: Documents) -> None:
    """The page as it is."""


Prepare = Callable[[Page, Documents], None]
DOCUMENT_SCREENS: dict[str, tuple[str, Callable[[Documents], str], Prepare]] = {
    "review page": ("eng-mad", lambda d: d.pending_request, nothing),
    "review page with the line dialog": ("eng-mad", lambda d: d.pending_request, open_a_dialog),
    "review page with the return dialog": (
        "eng-mad",
        lambda d: d.pending_request,
        open_the_return_dialog,
    ),
    "sourced request": ("eng-mad", lambda d: d.bought_request, nothing),
    "request status": ("tech-mad", lambda d: d.request_status, nothing),
    "order": ("mm-mad", lambda d: d.order, nothing),
    "receive form": ("wo-mad", lambda d: d.receive, nothing),
    # M9.3: what a project receiver works with (the technician, through the project only).
    "expected deliveries of a project": ("tech-mad", lambda d: d.site_expected, nothing),
    "receive form at a worksite": ("tech-mad", lambda d: d.site_receive, nothing),
    "worksite receipt": ("tech-mad", lambda d: d.site_receipt, nothing),
}


@pytest.mark.parametrize("viewport", ["phone", "desktop"])
@pytest.mark.parametrize("name", sorted(DOCUMENT_SCREENS))
def test_the_document_pages(
    browsing: Browsing, documents: Documents, name: str, viewport: str
) -> None:
    person, url, prepare = DOCUMENT_SCREENS[name]
    page = browsing.as_(person, viewport)
    page.goto(url(documents))
    expect(page.locator("main h1").first).to_be_visible()
    prepare(page, documents)
    found = audit_schemes(page, f"{name} {viewport}")
    assert not found, "\n".join(found)


def test_the_chart_library_loads_only_where_a_chart_is_drawn(browsing: Browsing) -> None:
    page = browsing.as_("admin", "desktop")
    seen: list[str] = []
    page.on("request", lambda request: seen.append(request.url))
    page.goto("/requests/")
    expect(page.locator("main h1").first).to_be_visible()
    assert not [u for u in seen if re.search(r"chart(\.umd|s)", u)], seen
    seen.clear()
    page.goto("/")
    settled(page)
    assert [u for u in seen if "chart.umd" in u], "the home page does not load the chart library"
