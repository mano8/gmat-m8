"""Each person chooses where and what they are told (M17.5, issue #75, ADR-0032).

One journey through the screens, the mail sink and the bell:

- the requester turns *My request approved* off by email and on in the application: the approval
  of their request mails them nothing, the bell shows one notification, and opening it leads to
  the request and clears the bell;
- *A request to review* is off for everyone and one reviewer turns it on by email for themselves:
  only they are mailed;
- an administrator then sets *never* for that reviewer, which does not stop what the reviewer
  turned on: their own choice wins in both directions (Q93).

Every setting goes back at the end, so the other flows meet the defaults. The mail sink says what
arrived, never what was left unsent: a later message of the same worker is the witness (the worker
claims in order; see ``test_notification_settings``).
"""

import re
from collections.abc import Iterator
from contextlib import contextmanager

from playwright.sync_api import Locator, Page, expect

from tests.e2e import pages
from tests.e2e.conftest import Browsing
from tests.e2e.mailbox import Mailbox
from tests.e2e.test_notification_settings import (
    DOMAIN,
    MATERIALS,
    NOTICE,
    mails_to,
    set_default,
    set_person,
)

TO_REVIEW = "REQUEST_TO_REVIEW"
APPROVED = "REQUEST_APPROVED"
REQUEST_PAGE = re.compile(r"/requests/[0-9a-f-]{36}/$")


def own_cell(page: Page, notice: str, channel: str) -> Locator:
    """The cell of *My notifications* that says what ``notice`` does on ``channel``."""
    row = page.locator(f"[data-testid=notice-state][data-notice={notice}]")
    return row.locator(f"td[data-channel={channel}]")


def set_own(page: Page, channel: str, choices: dict[str, str]) -> None:
    """The person's own choices on one channel (``email``, ``in-app``): ``on``, ``off`` or blank
    (as the administrators set it), from the channel's dialog of *My notifications*."""
    page.goto("/account/notifications/")
    page.get_by_test_id(f"choices-{channel}-open").click()
    dialog = page.get_by_test_id(f"choices-{channel}-dialog")
    expect(dialog.get_by_test_id(f"choices-{channel}-form")).to_be_visible()
    for notice, value in choices.items():
        dialog.locator(f"select[name={notice}]").select_option(value=value)
    pages.send_dialog(page, dialog, f"choices-{channel}-submit")
    expect(page.get_by_test_id("notice-state")).to_have_count(6)


def bell(page: Page) -> Locator:
    return page.get_by_test_id("notification-bell")


def read_everything(page: Page) -> None:
    """*Mark all as read*, so the bell counts only what the journey adds (the other flows leave
    notifications behind)."""
    page.goto("/notifications/")
    form = page.get_by_test_id("read-all-form")
    if form.count():
        with page.expect_navigation():
            form.get_by_role("button").click()
    expect(bell(page)).to_have_attribute("data-unread", "0")


@contextmanager
def channels_restored(browsing: Browsing) -> Iterator[None]:
    """Whatever the journey changed goes back, so the other flows meet the defaults."""
    try:
        yield
    finally:
        admin = browsing.as_("admin", "desktop")
        set_default(admin, NOTICE, turn_on=True)
        set_person(admin, "eng-mad", TO_REVIEW, "")
        set_own(browsing.as_("eng-mad", "desktop"), "email", {TO_REVIEW: ""})
        technician = browsing.as_("tech-mad", "desktop")
        set_own(technician, "email", {APPROVED: ""})
        set_own(technician, "in-app", {APPROVED: ""})


def test_each_person_chooses_where_and_what(browsing: Browsing, mailbox: Mailbox) -> None:
    admin = browsing.as_("admin", "desktop")
    technician = browsing.as_("tech-mad", "desktop")
    reviewer = browsing.as_("eng-mad", "desktop")
    with channels_restored(browsing):
        # The requester: an approval in the application only.
        set_own(technician, "email", {APPROVED: "off"})
        set_own(technician, "in-app", {APPROVED: "on"})
        expect(own_cell(technician, APPROVED, "EMAIL")).to_contain_text("Not sent")
        expect(own_cell(technician, APPROVED, "IN_APP")).to_contain_text("You chose this yourself.")
        read_everything(technician)

        # A request to review: off for everyone, on by email for one reviewer of their own choice.
        set_default(admin, NOTICE, turn_on=False)
        set_own(reviewer, "email", {TO_REVIEW: "on"})
        expect(own_cell(reviewer, TO_REVIEW, "EMAIL")).to_contain_text(
            "It overrides what the administrators set."
        )
        first = pages.submit_request(technician, "COL-MAD", {MATERIALS[0]: 2})
        message = mailbox.wait_for(f"eng-mad{DOMAIN}", containing=first)
        assert first in message["Subject"]
        technician.wait_for_timeout(15_000)  # the others would have been delivered by now
        assert mails_to(mailbox, "mm-mad", first) == []
        assert mails_to(mailbox, "admin", first) == []

        # An administrator's never for that reviewer does not stop what they turned on.
        set_person(admin, "eng-mad", TO_REVIEW, "NEVER")
        reviewer.goto("/account/notifications/")
        cell = own_cell(reviewer, TO_REVIEW, "EMAIL")
        expect(cell).to_contain_text("Sent")
        expect(cell).to_contain_text("It overrides what the administrators set.")
        pages.approve_all(reviewer, first)
        second = pages.submit_request(technician, "COL-MAD", {MATERIALS[1]: 2})
        mailbox.wait_for(f"eng-mad{DOMAIN}", containing=second)  # also the witness, queued later
        assert mails_to(mailbox, "tech-mad", first) == []  # the approval, not by email

        # ... but in the application: the bell counts it, opening it leads to the request.
        technician.goto("/requests/")
        expect(bell(technician)).to_have_attribute("data-unread", "1")
        expect(bell(technician)).to_have_attribute("aria-label", re.compile(r"1 unread"))
        bell(technician).click()
        technician.wait_for_url("**/notifications/")
        row = technician.get_by_test_id("notification").first
        expect(row).to_have_attribute("data-state", "unread")
        expect(row).to_have_attribute("data-notice", APPROVED)
        expect(row).to_contain_text(first)
        row.get_by_role("button", name="Open").click()
        technician.wait_for_url(REQUEST_PAGE)
        expect(technician.locator("main h1").first).to_have_text(first)
        expect(bell(technician)).to_have_attribute("data-unread", "0")
        technician.goto("/notifications/")
        expect(technician.get_by_test_id("notification").first).to_have_attribute(
            "data-state", "read"
        )
