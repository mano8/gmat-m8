"""The component library at 360 px (ADR-0027, M8b.1): no sideways scroll, targets of 44 px, no
error in the console, in both themes. It needs no stack: it draws ``manage styleguide`` from a
file, so it only runs where the CSS has been built (``static/dist/app.css``)."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from django.conf import settings
from django.core.management import call_command
from playwright.sync_api import Browser

from tests.e2e.conftest import PHONE

pytestmark = pytest.mark.skipif(
    not (settings.BASE_DIR / "static" / "dist" / "app.css").is_file(),
    reason="static/dist/app.css is not built",
)

CONTROLS = (
    "a[href], button, select, textarea, summary, "
    "input:not([type=hidden]):not([type=checkbox]):not([type=radio])"
)
TARGETS = """(controls) => [...document.querySelectorAll(controls)].filter((element) => {
  const box = element.getBoundingClientRect();
  return box.width > 0 && !element.closest('.sr-only')
    && (box.height < 43.5 || box.width < 43.5);
}).map((element) => element.tagName + ' ' + (element.textContent || '').trim().slice(0, 30))"""


@pytest.fixture(scope="module")
def page_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    output = tmp_path_factory.mktemp("styleguide")
    call_command("styleguide", output=str(output), verbosity=0)
    yield (Path(output) / "index.html").as_uri()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_style_guide_fits_a_phone(browser: Browser, page_url: str, scheme: str) -> None:
    context = browser.new_context(**PHONE, color_scheme=scheme)  # type: ignore[arg-type]
    page = context.new_page()
    problems: list[str] = []
    page.on(
        "console",
        lambda message: problems.append(message.text) if message.type == "error" else None,
    )
    page.on("requestfailed", lambda request: problems.append(request.url))
    page.goto(page_url)
    overflow = page.evaluate(
        "document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    assert overflow <= 0, "the page scrolls sideways at 360 px"
    assert page.evaluate(TARGETS, CONTROLS) == [], "targets under 44 px"
    assert problems == []
    context.close()
