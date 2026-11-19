"""Buttons keep their own width (M16, issue #72): a page's actions sit beside its title and the
main action of a phone form fills the row. The component once read a parameter Django also sets
inside every ``{% block %}``, so each button drawn with a page filled its row."""

from typing import cast

import pytest
from playwright.sync_api import Locator, Page, expect

from tests.e2e.conftest import Browsing
from tests.e2e.documents import Documents


def head_actions(page: Page) -> list[dict[str, float]]:
    """The width and top of every button in the page head's actions, with the head's width."""
    expect(page.locator("main .page-head .page-head-actions .btn").first).to_be_visible()
    return cast(
        list[dict[str, float]],
        page.evaluate(
            """() => {
              const head = document.querySelector('main .page-head').getBoundingClientRect();
              return [...document.querySelectorAll('main .page-head .page-head-actions .btn')]
                .map((b) => b.getBoundingClientRect())
                .filter((r) => r.width > 0)
                .map((r) => ({width: r.width, top: r.top, height: r.height, head: head.width}));
            }"""
        ),
    )


def fills_its_row(page: Page, button: Locator) -> bool:
    """The button is as wide as the content box of the element it sits in."""
    return cast(
        bool,
        button.evaluate(
            """(b) => {
              const cs = getComputedStyle(b.parentElement);
              const inner = b.parentElement.clientWidth
                - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
              return b.classList.contains('btn-block') && Math.abs(b.offsetWidth - inner) <= 2;
            }"""
        ),
    )


PAGE_HEADS = {
    "order": ("mm-mad", lambda d: d.order),
    "worklist": ("mm-mad", lambda d: "/purchasing/"),
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
    # Each as wide as its label, far from the head's width, and on one row: not a stack of bars.
    assert all(b["width"] < b["head"] / 2 for b in buttons), buttons
    assert (
        max(b["top"] for b in buttons) - min(b["top"] for b in buttons) < buttons[0]["height"] / 2
    )


def test_the_main_action_of_a_phone_form_still_fills_the_row(
    browsing: Browsing, documents: Documents
) -> None:
    warehouse = browsing.as_("wo-mad", "phone")
    warehouse.goto(documents.receive)
    post = warehouse.get_by_role("button", name="Post the receipt")
    expect(post).to_be_visible()
    assert fills_its_row(warehouse, post)

    manager = browsing.as_("mm-mad", "phone")
    manager.goto(documents.draft_order)
    issue = manager.get_by_role("button", name="Issue the order")
    expect(issue).to_be_visible()
    assert fills_its_row(manager, issue)
