"""The delivery badge of M7c (plan M8.2): an invitation shows "queued" while the worker holds
back, then "sent" once the worker delivers it to the mail sink, without the page reloading."""

import uuid

import pytest
from playwright.sync_api import expect

from tests.e2e import pages
from tests.e2e.conftest import Browsing
from tests.e2e.mailbox import Mailbox, worker_paused
from tests.live.conftest import Stack


@pytest.mark.parametrize("viewport", ["phone", "desktop"])
def test_invitation_badge_goes_from_queued_to_sent(
    browsing: Browsing, stack: Stack, mailbox: Mailbox, viewport: str
) -> None:
    address = f"badge-{uuid.uuid4().hex[:8]}@demo.example"
    admin = browsing.as_("admin", viewport)
    with worker_paused(stack):
        pages.invite(admin, address, "Badge Test", "Technician", branch="A-MAD · Alfa Madrid")
        admin.goto("/org/invitations/")
        row = admin.locator("tr, article, li", has_text=address).first
        badge = row.get_by_test_id("delivery-status")
        expect(badge).to_have_attribute("data-state", "queued")
        expect(badge).to_contain_text("Queued")
        # The page is not reloaded from here on: the badge polls and swaps itself.
        admin.evaluate("window.__kept = true")
    expect(badge).to_have_attribute("data-state", "sent", timeout=60_000)
    expect(badge).to_contain_text("Sent")
    assert admin.evaluate("window.__kept") is True
    message = mailbox.wait_for(address)
    assert "/invite/" in message["Text"]
