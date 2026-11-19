"""Buttons keep their own width (M16, issue #72): a page's actions sit beside its title and the
main action of a phone form fills the row. The component once read a parameter Django also sets
inside every ``{% block %}``, so each button drawn with a page filled its row."""

from typing import cast

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import Browsing
from tests.e2e.documents import Documents

# Wide enough for the longest label of a page head, far below a desktop's content width.
NARROW = 360


def head_actions(page: Page) -> list[tuple[float, float]]:
    """The (width, top) of every button in the page head's actions."""
    expect(page.locator("main .page-head .page-head-actions .btn").first).to_be_visible()
    return cast(
        list[tuple[float, float]],
        page.evaluate(
            """() => [...document.querySelectorAll('main .page-head .page-head-actions .btn')]
            .filter((b) => b.getBoundingClientRect().width > 0)
            .map((b) => [b.getBoundingClientRect().width, b.getBoundingClientRect().top])"""
        ),
    )


PAGE_HEADS = {
    "order": ("mm-mad", lambda d: d.order),
    "order-amendment": ("mm-mad", lambda d: f"{d.order}amendment/"),
    "project": ("admin", lambda d: d.site_project),
    "receipt": ("tech-mad", lambda d: d.site_receipt),
    "requests": ("tech-mad", lambda d: "/requests/"),
    "catalog": ("admin", lambda d: "/catalog/"),
}


@pytest.mark.parametrize("name", sorted(PAGE_HEADS))
def test_page_actions_keep_their_own_width_on_a_desktop(
    browsing: Browsing, documents: Documents, name: str
) -> None:
    person, url = PAGE_HEADS[name]
    page = browsing.as_(person, "desktop")
    page.goto(url(documents))
    buttons = head_actions(page)
    assert all(width <= NARROW for width, _ in buttons), buttons
    # Side by side: one row, not a stack of bars.
    assert max(top for _, top in buttons) - min(top for _, top in buttons) < 8, buttons


def test_the_main_action_of_a_phone_form_still_fills_the_row(
    browsing: Browsing, documents: Documents
) -> None:
    warehouse = browsing.as_("wo-mad", "phone")
    warehouse.goto(documents.receive)
    post = warehouse.get_by_role("button", name="Post the receipt")
    expect(post).to_be_visible()
    box = post.bounding_box()
    bar = warehouse.get_by_test_id("receipt-bar").bounding_box()
    assert box is not None and bar is not None
    assert box["width"] > bar["width"] * 0.9, (box, bar)

    manager = browsing.as_("mm-mad", "phone")
    manager.goto(documents.draft_order)
    issue = manager.get_by_role("button", name="Issue the order")
    expect(issue).to_be_visible()
    box = issue.bounding_box()
    bar = manager.get_by_test_id("issue-bar").bounding_box()
    assert box is not None and bar is not None
    assert box["width"] > bar["width"] * 0.9, (box, bar)
