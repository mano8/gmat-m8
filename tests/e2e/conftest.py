"""Fixtures for the end-to-end layer: Playwright against the running compose stack (plan §8.1, D).

The stack is started by the caller (``e2e.yaml``, or by hand with ``bash init.sh && docker
compose up -d --build --wait`` in ``docker_compose/gmat_m8``) and must have an **empty
database**: the session provisions it through the application (``provision.py``: the first
administrator accepts the one-time link in the browser, then ``seed_demo``). Nothing here starts
or builds the stack; ``docker compose`` is only used to look inside it.

Environment:

- ``E2E_BASE_URL``         public address of the stack (default ``https://localhost:8443``);
- ``E2E_COMPOSE_DIR``      the compose directory (default ``docker_compose/gmat_m8``);
- ``E2E_COMPOSE_PROJECT``  the compose project name (``-p``) when it is not the default;
- ``E2E_STATE_FILE``       a JSON file where the session keeps the authenticator secrets it
  enrolled. With it, a second run on the same stack skips the provisioning (the data of the
  first run stays, so only a fresh stack is guaranteed to pass); without it a stack that already
  has accounts is refused;
- ``E2E_REQUIRED=1``       a stack that does not answer fails the run instead of skipping it.

Every page opened through ``browsing`` is watched: a Content-Security-Policy violation, an
uncaught script error or a 5xx answer fails the test that caused it.
"""

import contextlib
import json
import os
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Browser, BrowserContext, Page

from tests.e2e.mailbox import Mailbox
from tests.e2e.provision import Person, provision, sign_in
from tests.e2e.totp import Authenticator
from tests.live.conftest import Stack

ROOT = Path(__file__).resolve().parents[2]
BASE_URL = os.environ.get("E2E_BASE_URL", "https://localhost:8443")

PHONE: dict[str, Any] = {
    "viewport": {"width": 360, "height": 740},
    "device_scale_factor": 2,
    "is_mobile": True,
    "has_touch": True,
}
DESKTOP: dict[str, Any] = {"viewport": {"width": 1280, "height": 800}}
VIEWPORTS = {"phone": PHONE, "desktop": DESKTOP}

# Reports the browser's own CSP events to the test; ``__gmatReport`` is exposed by the context.
WATCH_SCRIPT = """
document.addEventListener('securitypolicyviolation', (event) => {
  window.__gmatReport('csp', event.violatedDirective + ' ' + event.blockedURI
    + ' ' + (event.sourceFile || '') + ':' + event.lineNumber);
});
"""


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Iterator[None]:
    """Keeps each phase's report on the item, so ``browsing`` can tell a failed test."""
    report = yield
    setattr(item, f"report_{call.when}", report)
    return report


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "/e2e/" in item.nodeid.replace("\\", "/"):
            item.add_marker(pytest.mark.e2e)


@pytest.fixture(scope="session")
def stack() -> Iterator[Stack]:
    compose_dir = Path(os.environ.get("E2E_COMPOSE_DIR", ROOT / "docker_compose" / "gmat_m8"))
    candidate = Stack(
        base_url=BASE_URL,
        compose_dir=compose_dir,
        compose_files=[],
        project=os.environ.get("E2E_COMPOSE_PROJECT", ""),
    )
    required = os.environ.get("E2E_REQUIRED") == "1"
    deadline = time.monotonic() + (60 if required else 3)
    last_error = ""
    while time.monotonic() < deadline:
        try:
            if candidate.get("/healthz/", timeout=5).status == 200:
                yield candidate
                return
        except OSError as error:
            last_error = str(error)
        time.sleep(1)
    message = f"no compose stack answers at {candidate.base_url} ({last_error})"
    if required:
        pytest.fail(message)
    pytest.skip(message)


@pytest.fixture(scope="session")
def people(stack: Stack, browser: Browser) -> dict[str, Person]:
    """The people the flows sign in as, with their authenticators."""
    state = os.environ.get("E2E_STATE_FILE")
    if state and Path(state).is_file():
        secrets: dict[str, str | None] = json.loads(Path(state).read_text(encoding="utf-8"))
        return {
            key: Person(key, authenticator=Authenticator(secret) if secret else None)
            for key, secret in secrets.items()
        }
    found = provision(stack, browser, BASE_URL)
    if state:
        Path(state).write_text(
            json.dumps(
                {k: p.authenticator.secret if p.authenticator else None for k, p in found.items()}
            ),
            encoding="utf-8",
        )
    return found


@dataclass
class Browsing:
    """Opens pages for the people of the demo organization, one context per page."""

    browser: Browser
    people: dict[str, Person]
    states: dict[str, Any] = field(default_factory=dict)
    contexts: list[BrowserContext] = field(default_factory=list)
    pages: list[Page] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def _context(
        self, viewport: str, state: Any = None, *, javascript: bool = True
    ) -> BrowserContext:
        context = self.browser.new_context(
            base_url=BASE_URL,
            ignore_https_errors=True,  # Caddy's internal CA
            locale="en-US",
            storage_state=state,
            java_script_enabled=javascript,
            **VIEWPORTS[viewport],
        )
        context.expose_function(
            "__gmatReport", lambda kind, text: self.problems.append(f"{kind}: {text}")
        )
        context.add_init_script(WATCH_SCRIPT)
        context.set_default_timeout(15_000)
        self.contexts.append(context)
        return context

    def _watch(self, page: Page) -> Page:
        self.pages.append(page)
        page.on("pageerror", lambda error: self.problems.append(f"script error: {error}"))
        page.on(
            "response",
            lambda response: (
                self.problems.append(f"{response.status} {response.url}")
                if response.status >= 500
                else None
            ),
        )
        return page

    def screenshots(self, name: str) -> None:
        """What every page looked like, for the person reading a failed run."""
        folder = ROOT / "test-results"
        folder.mkdir(exist_ok=True)
        for number, page in enumerate(self.pages):
            with contextlib.suppress(Exception):  # a closed page has nothing to show
                page.screenshot(path=folder / f"{name}-{number}.png", full_page=True)

    def anonymous(self, viewport: str = "desktop") -> Page:
        return self._watch(self._context(viewport).new_page())

    def fresh_sign_in(self, key: str, viewport: str = "desktop") -> Page:
        """A new session of ``key``, through the sign-in page (not shared with other tests)."""
        page = self.anonymous(viewport)
        sign_in(page, self.people[key])
        return page

    def as_(self, key: str, viewport: str = "desktop") -> Page:
        """A page signed in as ``key``; the session is made once and reused by later tests."""
        if key not in self.states:
            first = self.anonymous("desktop")
            sign_in(first, self.people[key])
            self.states[key] = first.context.storage_state()
            first.context.close()
        return self._watch(self._context(viewport, self.states[key]).new_page())

    def without_javascript(self, key: str, viewport: str = "desktop") -> Page:
        """Like ``as_``, in a browser with scripts switched off: what a form does on its own."""
        self.as_(key, viewport).context.close()  # makes the session if it is not there yet
        context = self._context(viewport, self.states[key], javascript=False)
        return self._watch(context.new_page())


@pytest.fixture(scope="session")
def _sessions(browser: Browser, people: dict[str, Person]) -> dict[str, Any]:
    return {}


@pytest.fixture
def browsing(
    request: pytest.FixtureRequest,
    browser: Browser,
    people: dict[str, Person],
    _sessions: dict[str, Any],
) -> Iterator[Browsing]:
    opened = Browsing(browser, people, _sessions)
    yield opened
    report = getattr(request.node, "report_call", None)
    if report is not None and report.failed:
        opened.screenshots(re.sub(r"\W+", "-", request.node.name))
    for context in opened.contexts:
        context.close()
    assert not opened.problems, "the browser reported:\n" + "\n".join(opened.problems)


@pytest.fixture(scope="session")
def mailbox(stack: Stack) -> Mailbox:
    return Mailbox(stack)


_DOCUMENTS: list[Any] = []


@pytest.fixture
def documents(browsing: Browsing) -> Any:
    """The pages of ``tests.e2e.documents``, made through the screens the first time a test asks
    for them and kept for the rest of the run."""
    if not _DOCUMENTS:
        from tests.e2e.documents import build  # it imports this module

        _DOCUMENTS.append(build(browsing))
    return _DOCUMENTS[0]
