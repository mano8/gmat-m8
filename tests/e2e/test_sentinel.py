"""The browser-level sentinel test (plan §5, level 3; M8.2): a real browser follows an invitation,
a password reset and an email-change link, loads assets, makes htmx requests and goes back and
forward. No token and no password may then appear in any log stream (Caddy, Gunicorn and Django
in ``web``, the worker), and none may have been sent to the server in a ``Referer`` or
``HX-Current-URL`` header."""

import re
import uuid
from urllib.parse import urlsplit

from playwright.sync_api import BrowserContext, Page, Request, expect

from tests.e2e import pages
from tests.e2e.conftest import Browsing
from tests.e2e.mailbox import Mailbox
from tests.live.conftest import Stack

URL_PARTS = re.compile(r"[A-Za-z0-9_-]{16,}")


class Traffic:
    """Every request a context makes, to look at its headers afterwards."""

    def __init__(self, context: BrowserContext) -> None:
        self.requests: list[Request] = []
        context.on("request", lambda request: self.requests.append(request))

    def leaks(self, secrets: list[str]) -> list[str]:
        found = []
        for request in self.requests:
            headers = request.all_headers()
            for name in ("referer", "hx-current-url", "origin"):
                value = headers.get(name, "")
                found += [
                    f"{name}={value} of {request.url} carries {secret[:10]}..."
                    for secret in secrets
                    if secret in value
                ]
        return found


def tokens_of(link: str, *, after_user: bool = False) -> list[str]:
    """The secrets a link carries: its token-like path segments, and their halves. A reset key
    reads ``<user id>-<timestamp>-<hash>``: the user id in front is not a secret."""
    found = []
    for segment in urlsplit(link).path.split("/"):
        if not URL_PARTS.fullmatch(segment):
            continue
        key = segment.split("-", 1)[1] if after_user else segment
        found += [key, *(part for part in key.split("-") if len(part) >= 12)]
    return found


def sign_in(page: Page, address: str, password: str) -> None:
    page.goto("/accounts/login/")
    page.fill("input[name=login]", address)
    page.fill("input[name=password]", password)
    page.click("main form button[type=submit]")
    page.wait_for_url(lambda url: "/accounts/login/" not in url)
    expect(page.get_by_test_id("dashboard")).to_be_visible()
    expect(page.get_by_text("Loading…")).to_have_count(0)


def test_secrets_never_reach_a_log_or_a_referer(
    browsing: Browsing, stack: Stack, mailbox: Mailbox
) -> None:
    run = uuid.uuid4().hex
    password, new_password = f"Sentinel-{run}-pw", f"Sentinel-{run[::-1]}-pw"
    address, new_address = f"sentinel-{run[:8]}@demo.example", f"sentinel-{run[8:16]}@demo.example"
    secrets = [password, new_password]
    seen: set[str] = set()

    admin = browsing.as_("admin")
    pages.invite(admin, address, "Sentinel Person", "Technician", branch="A-MAD · Alfa Madrid")

    # Invitation: the link leaves the address bar at once; the person sets a password and signs in.
    person = browsing.anonymous()
    traffic = Traffic(person.context)
    link = mailbox.link(mailbox.wait_for(address, ignore=seen, containing="/invite/"), "/invite/")
    secrets += tokens_of(link)
    person.goto(link)
    expect(person).to_have_url(re.compile(r"/invite/accept/$"))
    person.fill("#id_password1", password)
    person.fill("#id_password2", password)
    person.click("main form button[type=submit]")
    person.wait_for_url("**/accounts/login/")
    sign_in(person, address, password)

    # Assets, htmx requests (a filtered list), back and forward.
    person.goto("/catalog/")
    person.fill("#id_q", "cable")
    expect(person).to_have_url(re.compile(r"q=cable"))
    pages.back(person)
    pages.forward(person)
    person.goto("/requests/")
    pages.back(person)

    # Password reset: the link redirects to a token-less page; the new password is typed there.
    person.context.clear_cookies()
    person.goto("/accounts/password/reset/")
    person.fill("input[name=email]", address)
    person.click("main form button[type=submit]")
    key_path = "/password/reset/key/"
    reset = mailbox.link(mailbox.wait_for(address, ignore=seen, containing=key_path), key_path)
    secrets += tokens_of(reset, after_user=True)
    person.goto(reset)
    person.fill("#id_password1", new_password)
    person.fill("#id_password2", new_password)
    person.click("main form button[type=submit]")
    person.wait_for_url(re.compile(r"/(done|login)/"))
    # The owner is told that the password changed (M8.3); a notice holds no link to use.
    notice = mailbox.wait_for(address, ignore=seen)
    assert key_path not in notice["Text"]
    pages.back(person)
    pages.forward(person)
    sign_in(person, address, new_password)

    # Email change: started by the administrator, confirmed from the new address's link.
    admin.goto("/org/people/?q=" + address)
    admin.get_by_role("link", name=address).click()
    admin.fill("#id_new_email", new_address)
    admin.get_by_role("button", name="Change").click()
    change_path = "/account/email-change/"
    change = mailbox.link(
        mailbox.wait_for(new_address, ignore=seen, containing=change_path), change_path
    )
    secrets += tokens_of(change)
    confirmer = browsing.anonymous()
    traffic_2 = Traffic(confirmer.context)
    confirmer.goto(change)
    expect(confirmer).to_have_url(re.compile(r"/account/email-change/confirm/$"))
    confirmer.get_by_role("button", name="Confirm").click()
    expect(confirmer).to_have_url(re.compile(r"/accounts/login/$"))
    pages.back(confirmer)
    pages.forward(confirmer)

    assert not (leaks := traffic.leaks(secrets) + traffic_2.leaks(secrets)), leaks
    for service in ("caddy", "web", "worker"):
        logged = stack.logs(service)
        for secret in secrets:
            assert secret not in logged, f"{service} logged a secret ({secret[:12]}...)"
    # The access log exists and shows the redaction at work.
    assert "/invite/REDACTED" in stack.logs("caddy").replace("\\/", "/")
