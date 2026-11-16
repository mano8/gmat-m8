"""The reviewer flow of M7b (plan M8.2): a request sent to an engineer, who edits a line of the
submitted request and a line of the approved one; the requester sees the original and the edits."""

import re

import pytest
from playwright.sync_api import expect

from tests.e2e import pages
from tests.e2e.conftest import Browsing

FIRST, SECOND = "CAB-H07VK-4-AZ", "CAB-H07VK-4-GR"


@pytest.mark.parametrize("viewport", ["phone", "desktop"])
def test_edits_of_an_assigned_request(browsing: Browsing, viewport: str) -> None:
    technician = browsing.as_("tech-mad", viewport)
    number = pages.submit_request(
        technician,
        "COL-MAD",
        {FIRST: 10, SECOND: 5},
        priority="Urgent",
        reviewer="Elena Navarro",
    )
    status_page = technician.url
    expect(technician.get_by_text("Sent to")).to_be_visible()
    expect(technician.locator("dd", has_text="Elena Navarro")).to_be_visible()

    engineer = browsing.as_("eng-mad", viewport)
    pages.open_in_inbox(engineer, number)
    expect(engineer.get_by_test_id("assignee")).to_contain_text("Elena Navarro")
    # A line of the submitted request: change it and approve it in one step.
    pages.change_line(engineer, FIRST, "8", "Only 8 m fit the run", approve=True)
    expect(pages.review_line(engineer, FIRST)).to_contain_text("Edited")
    expect(pages.review_line(engineer, FIRST)).to_contain_text("Approved")
    pages.approve_all(engineer, number)
    # A line of the approved request: the change is approved with it.
    pages.open_in_inbox(engineer, number, status="APPROVED")
    pages.change_line(engineer, SECOND, "3", "The client cut the order")
    expect(pages.review_line(engineer, SECOND)).to_contain_text("Edited")

    technician.goto(status_page)
    changes = technician.get_by_test_id("request-changes")
    expect(changes.get_by_test_id("changed-line")).to_have_count(2)
    first = changes.get_by_test_id("changed-line").filter(has_text=FIRST)
    expect(first.locator("[data-field=submitted]")).to_contain_text("10 M")
    expect(first.get_by_test_id("line-edit")).to_have_count(1)
    expect(first.locator("[data-field=reason]")).to_have_text("Only 8 m fit the run")
    expect(first.locator("[data-field=change]")).to_contain_text(
        re.compile(r"10 M\s*→.*· 8 M", re.S)
    )
    second = changes.get_by_test_id("changed-line").filter(has_text=SECOND)
    expect(second.locator("[data-field=submitted]")).to_contain_text("5 M")
    expect(second.locator("[data-field=change]")).to_contain_text(
        re.compile(r"5 M\s*→.*· 3 M", re.S)
    )
    # The requester's lines show the current quantities.
    rows = technician.get_by_test_id("request-lines")
    expect(rows.locator("tr", has_text=FIRST).locator("td").nth(2)).to_contain_text("8 M")
    expect(rows.locator("tr", has_text=SECOND).locator("td").nth(2)).to_contain_text("3 M")


# Materials no other journey buys or reviews (the ledgers never meet).
LINES = {
    "phone": ("CAB-H07VK-10-AZ", "CAB-H07VK-10-MA", "CAB-H07VK-10-NE"),
    "desktop": ("CAB-H07VK-1,5-AZ", "CAB-H07VK-1,5-MA", "CAB-H07VK-1,5-NE"),
}


@pytest.mark.parametrize("viewport", ["phone", "desktop"])
def test_deciding_line_by_line_keeps_the_place(browsing: Browsing, viewport: str) -> None:
    """M14.2: each decision opens the page at the next pending line, never at its top, and the
    next Tab reaches that line's quantity; the last one shows the request's new status."""
    first, second, third = LINES[viewport]
    technician = browsing.as_("tech-mad", viewport)
    number = pages.submit_request(technician, "COL-MAD", {first: 6, second: 5, third: 4})
    engineer = browsing.as_("eng-mad", viewport)
    pages.open_in_inbox(engineer, number)

    pages.review_line(engineer, first).get_by_test_id("approve-line").click()
    expect(engineer).to_have_url(re.compile(r"/examine/#line-2$"))
    next_line = pages.review_line(engineer, second)
    expect(next_line).to_be_in_viewport()
    assert engineer.evaluate("window.scrollY") > 0, "the page went back to its top"
    engineer.keyboard.press("Tab")
    expect(next_line.locator("input[name=approved_qty]")).to_be_focused()
    expect(pages.review_line(engineer, first)).to_contain_text("Approved")

    pages.review_line(engineer, second).get_by_test_id("approve-line").click()
    expect(engineer).to_have_url(re.compile(r"/examine/#line-3$"))
    expect(pages.review_line(engineer, third)).to_be_in_viewport()

    pages.review_line(engineer, third).get_by_test_id("approve-line").click()
    expect(engineer).to_have_url(re.compile(r"/examine/$"))
    expect(engineer.locator("main")).to_contain_text("Approved")
    expect(engineer.get_by_test_id("decide-bar")).to_have_count(0)


@pytest.mark.parametrize("viewport", ["phone", "desktop"])
def test_approve_all_from_the_top_of_the_page(browsing: Browsing, viewport: str) -> None:
    """M14.2: the head of the review page asks for confirmation, then approves every line."""
    first, second, third = LINES[viewport]
    technician = browsing.as_("tech-mad", viewport)
    number = pages.submit_request(technician, "COL-MAD", {first: 3, second: 2, third: 1})
    engineer = browsing.as_("eng-mad", viewport)
    pages.open_in_inbox(engineer, number)
    # One decision first: the dialog counts only what still waits.
    pages.review_line(engineer, first).get_by_test_id("approve-line").click()
    expect(engineer).to_have_url(re.compile(r"/examine/#line-2$"))
    engineer.evaluate("window.scrollTo(0, 0)")
    engineer.get_by_test_id("approve-all-open").click()
    dialog = engineer.get_by_test_id("approve-all-dialog")
    expect(dialog).to_contain_text("2 lines wait for a decision")
    pages.send_dialog(engineer, dialog, "approve-all-confirm")
    expect(engineer).to_have_url(pages.EXAMINE_URL)
    expect(engineer.locator("main")).to_contain_text("Approved")
    expect(engineer.get_by_test_id("approve-all-open")).to_have_count(0)
    for reference in (first, second, third):
        expect(pages.review_line(engineer, reference)).to_contain_text("Approved")
