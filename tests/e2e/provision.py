"""Brings a fresh compose stack to the state the browser tests start from (plan M8.2).

Nothing is written to the database behind the application's back. The first group
administrator comes from ``manage bootstrap_admin`` and accepts the one-time link in a real
browser, enrolls an authenticator, and is then the operator of ``manage seed_demo`` (the A/B/C
organization of plan §3.2, one user per role, projects, ~200 materials, 5 suppliers). The
privileged demo users enroll their authenticators the same way, through the pages.

``seed_demo`` refuses production settings, so it runs in a one-off ``web`` container with the
development settings (same database, same image); the stack itself keeps serving with the
production ones.
"""

import re
import time
from dataclasses import dataclass

from playwright.sync_api import Browser, Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from tests.e2e.totp import STEP, Authenticator
from tests.live.conftest import Stack

PASSWORD = "Demo-e2e-pass-2026!"
ADMIN_EMAIL = "admin@demo.example"
DEMO_DOMAIN = "demo.example"
# The people the flows sign in as. ``admin`` is the bootstrap administrator; the others are the
# users ``seed_demo`` creates (``apps/core/demo.py``).
PRIVILEGED = ("admin", "mm-mad", "eng-mad")
UNPRIVILEGED = ("tech-mad", "wo-mad", "site-manager", "partner")


@dataclass
class Person:
    key: str  # the local part of the address
    password: str = PASSWORD
    authenticator: Authenticator | None = None

    @property
    def email(self) -> str:
        return f"{self.key}@{DEMO_DOMAIN}"


class NotFresh(RuntimeError):
    """The stack already has accounts: the tests need an empty database."""


def manage(stack: Stack, *args: str, dev: bool = False) -> str:
    """One management command in a throwaway ``web`` container; returns its standard output."""
    command = ["run", "--rm", "-T"]
    if dev:
        command += ["-e", "DJANGO_SETTINGS_MODULE=config.settings.dev"]
    result = stack.compose(*command, "web", "manage", *args)
    return result.stdout


def require_fresh(stack: Stack) -> None:
    users = stack.sql("SELECT count(*) FROM accounts_user")
    if users != "0":
        raise NotFresh(
            f"the stack already has {users} account(s). The browser tests start from an empty "
            "database: `docker compose down --volumes && docker compose up -d --build --wait`."
        )


def bootstrap_link(stack: Stack) -> str:
    out = manage(
        stack,
        "bootstrap_admin",
        "--email",
        ADMIN_EMAIL,
        "--full-name",
        "Adela Arias",
        "--company-code",
        "A",
        "--company-name",
        "Instalaciones Alfa SA",
    )
    match = re.search(r"https?://\S+/invite/[\w-]+/", out)
    if match is None:
        raise RuntimeError("bootstrap_admin printed no invitation link")
    return match.group(0)


def sign_in(page: Page, person: Person) -> None:
    """Password, then the authenticator code when the account has one."""
    page.goto("/accounts/login/")
    page.fill("input[name=login]", person.email)
    page.fill("input[name=password]", person.password)
    page.click("main form button[type=submit]")
    page.wait_for_url(lambda url: "/accounts/login/" not in url)
    if person.authenticator is not None and page.locator("input[name=code]").count():
        _enter_code(page, person.authenticator)
    page.wait_for_load_state()


def _enter_code(page: Page, authenticator: Authenticator) -> None:
    """The server takes a code once per 30 s step, also from an earlier run of the suite: a
    refused code is tried again in the next step."""
    for attempt in (1, 2):
        page.fill("input[name=code]", authenticator.next_code())
        page.click("main form button[type=submit]")
        try:
            page.wait_for_url(lambda url: "/2fa/" not in url, timeout=5_000)
            return
        except PlaywrightTimeoutError:
            if attempt == 2:
                raise
            wait_for_next_step()


def accept_invitation(page: Page, link: str, person: Person) -> None:
    page.goto(link)
    page.fill("#id_password1", person.password)
    page.fill("#id_password2", person.password)
    page.click("main form button[type=submit]")
    page.wait_for_url("**/accounts/login/")


def enroll_authenticator(page: Page, person: Person) -> None:
    """Sign in with the password, activate an authenticator app through its page."""
    sign_in(page, person)
    page.goto("/accounts/2fa/totp/activate/")
    page.fill("input[name=password]", person.password)  # the reauthentication step
    page.click("main form button[type=submit]")
    page.wait_for_selector("#authenticator_secret")
    person.authenticator = Authenticator(page.input_value("#authenticator_secret"))
    page.fill("input[name=code]", person.authenticator.next_code())
    page.click("main form button[type=submit]")
    page.wait_for_url("**/accounts/2fa/recovery-codes/")


def wait_for_next_step() -> None:
    """The server takes each authenticator code once; the enrollment codes are spent."""
    time.sleep(STEP - time.time() % STEP + 0.5)


def seed(stack: Stack) -> None:
    manage(
        stack,
        "seed_demo",
        "--operator",
        ADMIN_EMAIL,
        "--password",
        PASSWORD,
        dev=True,
    )


def provision(stack: Stack, browser: Browser, base_url: str) -> dict[str, Person]:
    require_fresh(stack)
    people = {key: Person(key) for key in (*PRIVILEGED, *UNPRIVILEGED)}
    admin = people["admin"]
    context = browser.new_context(base_url=base_url, ignore_https_errors=True, locale="en-US")
    try:
        page = context.new_page()
        accept_invitation(page, bootstrap_link(stack), admin)
        enroll_authenticator(page, admin)
        seed(stack)
        for key in PRIVILEGED[1:]:
            context.clear_cookies()
            enroll_authenticator(page, people[key])
    finally:
        context.close()
    wait_for_next_step()
    return people
