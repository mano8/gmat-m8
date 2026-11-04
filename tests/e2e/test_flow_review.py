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
