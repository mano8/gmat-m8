"""Group administrators choose which business emails are sent (M15.4, issue #70, ADR-0031).

One journey through the screens and the mail sink: the administrator turns *A request to review*
off for everyone and to *always* for one reviewer; a technician submits a request and only that
reviewer's message reaches Mailpit; the technician turns business email off and the approval of
their request sends nothing, whatever the administrator set. Every setting goes back at the end,
so the other flows keep the defaults.

The mail sink says what arrived, never what was left unsent: a later message of the same worker is
the witness. The worker claims in order, so once the witness is in the sink what was queued before
it would be there too.
"""

import re
from collections.abc import Iterator
from contextlib import contextmanager

from playwright.sync_api import Page, expect

from tests.e2e import pages
from tests.e2e.conftest import Browsing
from tests.e2e.mailbox import Mailbox

DOMAIN = "@demo.example"
NOTICE = "request-to-review"
MATERIALS = ("CAB-H07VK-4-MA", "CAB-H07VK-4-NE")


def set_default(page: Page, notice: str, *, turn_on: bool) -> None:
    """The administration page's dialog of one email: turn it on or off for everyone."""
    page.goto("/org/notifications/")
    row = page.locator(f"[data-testid=notice-row][data-notice={notice.upper().replace('-', '_')}]")
    page.get_by_test_id(f"notice-{notice}-open").click()
    dialog = page.get_by_test_id(f"notice-{notice}-dialog")
    expect(dialog.get_by_test_id("consequences")).to_be_visible()
    pages.send_dialog(page, dialog, f"notice-{notice}-submit")
    expected = "Sent" if turn_on else "Not sent"
    expect(row.get_by_test_id("notice-state")).to_have_text(re.compile(expected))


def set_person(page: Page, account: str, notice: str, mode: str) -> None:
    """A person's page: their own setting for one email (``FOLLOW``, ``ALWAYS`` or ``NEVER``)."""
    page.goto(f"/org/people/?q={account}")
    page.get_by_role("link", name=f"{account}{DOMAIN}").click()
    page.get_by_test_id("notices-open").click()
    dialog = page.get_by_test_id("notices-dialog")
    expect(dialog.get_by_test_id("notices-form")).to_be_visible()
    dialog.locator(f"select[name={notice}]").select_option(value=mode)
    pages.send_dialog(page, dialog, "notices-submit")
    state = page.get_by_test_id("person-notices").get_by_test_id("notice-state")
    expect(state).to_have_count(6)


def set_business_email(page: Page, *, on: bool) -> None:
    """The person's own switch on ``/account/notifications/``."""
    page.goto("/account/notifications/")
    form = page.get_by_test_id("notification-settings")
    box = form.locator("input[name=business_email]")
    box.set_checked(on)
    form.get_by_role("button", name="Save").click()
    page.wait_for_url("**/account/notifications/")
    expect(
        page.get_by_test_id("notification-settings").locator("input[name=business_email]")
    ).to_be_checked(checked=on)


def mails_to(mailbox: Mailbox, account: str, number: str) -> list[str]:
    """Messages about ``number`` that reached ``account``'s address, as To or Bcc."""
    return [
        message["ID"]
        for message in mailbox.messages()
        if number in message.get("Subject", "")
        and f"{account}{DOMAIN}" in mailbox.recipients(message)
    ]


@contextmanager
def settings_restored(browsing: Browsing) -> Iterator[None]:
    """Whatever the journey changed goes back, so the other flows meet the defaults."""
    try:
        yield
    finally:
        admin = browsing.as_("admin", "desktop")
        set_default(admin, NOTICE, turn_on=True)
        set_person(admin, "eng-mad", "REQUEST_TO_REVIEW", "")
        set_business_email(browsing.as_("tech-mad", "desktop"), on=True)


def test_the_administrator_chooses_what_is_sent(browsing: Browsing, mailbox: Mailbox) -> None:
    admin = browsing.as_("admin", "desktop")
    technician = browsing.as_("tech-mad", "desktop")
    reviewer = browsing.as_("eng-mad", "desktop")
    with settings_restored(browsing):
        # Off for everyone, always for one reviewer.
        set_default(admin, NOTICE, turn_on=False)
        set_person(admin, "eng-mad", "REQUEST_TO_REVIEW", "ALWAYS")
        # The technician sees what applies to them, read-only.
        technician.goto("/account/notifications/")
        own = technician.get_by_test_id("own-notices")
        expect(own).to_contain_text("A request to review")

        first = pages.submit_request(technician, "COL-MAD", {MATERIALS[0]: 2})
        message = mailbox.wait_for(f"eng-mad{DOMAIN}", containing=first)
        assert first in message["Subject"]
        technician.wait_for_timeout(15_000)  # the others would have been delivered by now
        assert mails_to(mailbox, "mm-mad", first) == []
        assert mails_to(mailbox, "admin", first) == []

        # The requester's own refusal is never overridden: the approval sends nothing.
        set_business_email(technician, on=False)
        pages.approve_all(reviewer, first)
        second = pages.submit_request(technician, "COL-MAD", {MATERIALS[1]: 2})
        mailbox.wait_for(f"eng-mad{DOMAIN}", containing=second)  # the witness, queued later
        assert mails_to(mailbox, "tech-mad", first) == []
        assert mails_to(mailbox, "tech-mad", second) == []
