"""The list toolbar of M7d on long lists (plan M8.2): filter, page size, sort, page, and the
Back button restoring the filtered page as a whole page, not a fragment. Phone and desktop."""

import re

import pytest
from playwright.sync_api import Locator, Page, expect

from tests.e2e import pages
from tests.e2e.conftest import Browsing

DASH = "\N{EN DASH}"  # the pager writes "Showing 1, 10 of 204" with it


def showing(page: Page, first: int, last: int) -> Locator:
    return page.locator("main").get_by_text(re.compile(rf"Showing {first}{DASH}{last} of"))


def assert_whole_page(page: Page, heading: str) -> None:
    """Back restored a full document (layout, skip link, heading), not a bare fragment."""
    expect(page.locator("a[href='#main']")).to_have_count(1)
    expect(page.locator("main h1").first).to_have_text(heading)


def sort_descending(page: Page, viewport: str) -> None:
    """By name, descending: the sort select on a phone, the column header (twice) from sm up."""
    if viewport == "phone":
        page.select_option("#list-filter-sort", label="Name (descending)")
        return
    header = page.locator("main thead").get_by_role("link", name=re.compile("^Name"))
    header.click()
    expect(page).to_have_url(re.compile(r"sort=name"))
    header.click()


@pytest.mark.parametrize("viewport", ["phone", "desktop"])
def test_catalog_toolbar_and_back(browsing: Browsing, viewport: str) -> None:
    page = browsing.as_("mm-mad", viewport)
    page.goto("/catalog/")
    rows = page.locator("main tbody tr")
    total = page.locator("main [data-list-count]").first.inner_text().strip()

    default_size = page.locator("#list-filter-per-page").input_value()

    # Filter: the URL carries it, the list shrinks, the document stays the same one.
    pages.filter_list(page, "#id_q", "cable")
    expect(page).to_have_url(re.compile(r"[?&]q=cable"))
    expect(page.locator("main [data-list-count]").first).not_to_have_text(total)

    # Page size.
    page.select_option("#list-filter-per-page", "10")
    expect(page).to_have_url(re.compile(r"per_page=10"))
    expect(rows).to_have_count(10)
    expect(showing(page, 1, 10)).to_be_visible()
    unsorted_first = rows.first.inner_text()

    # Sort.
    sort_descending(page, viewport)
    expect(page).to_have_url(re.compile(r"sort=-name"))
    expect(showing(page, 1, 10)).to_be_visible()
    sorted_first = rows.first.inner_text()
    assert sorted_first != unsorted_first

    # Page (a plain link).
    page.get_by_role("link", name="Next").click()
    expect(page).to_have_url(re.compile(r"page=2"))
    expect(showing(page, 11, 20)).to_be_visible()
    page2_first = rows.first.inner_text()
    assert page2_first not in (sorted_first, unsorted_first)

    # Back: page 1 of the sorted, sized, filtered list, as a whole page.
    page.go_back()
    expect(page).not_to_have_url(re.compile(r"page=2"))
    expect(page).to_have_url(re.compile(r"sort=-name"))
    assert_whole_page(page, "Catalog")
    expect(showing(page, 1, 10)).to_be_visible()
    expect(page.locator("#id_q")).to_have_value("cable")
    expect(page.locator("#list-filter-per-page")).to_have_value("10")
    expect(rows.first).to_have_text(sorted_first)

    # Back again: the sort, then the page size, then the filter come off, one step at a time.
    # (From sm up the descending order took two clicks on the header: two history entries.)
    for _ in range(2 if viewport == "desktop" else 1):
        page.go_back()
        assert_whole_page(page, "Catalog")
    expect(page).not_to_have_url(re.compile(r"sort=-name"))
    expect(page.locator("#list-filter-per-page")).to_have_value("10")
    expect(rows.first).to_have_text(unsorted_first)
    page.go_back()
    assert_whole_page(page, "Catalog")
    expect(page.locator("#list-filter-per-page")).to_have_value(default_size)
    expect(page.locator("#id_q")).to_have_value("cable")
    page.go_back()
    assert_whole_page(page, "Catalog")
    expect(page.locator("#id_q")).to_have_value("")
    expect(page.locator("main [data-list-count]").first).to_have_text(total)


@pytest.mark.parametrize("viewport", ["phone", "desktop"])
def test_audit_toolbar_and_back(browsing: Browsing, viewport: str) -> None:
    page = browsing.as_("admin", viewport)
    page.goto("/audit/")
    rows = page.get_by_test_id("audit-row")
    default = page.locator("#list-filter-per-page").input_value()
    size = 25 if default == "10" else 10  # a size the list is not showing, and a filter that
    page.select_option("#list-filter-per-page", str(size))  # still leaves more than one page
    expect(page).to_have_url(re.compile(rf"per_page={size}"))
    expect(rows).to_have_count(size)
    pages.filter_list(page, "#id_q", "material")
    expect(page).to_have_url(re.compile(r"q=material"))
    expect(showing(page, 1, size)).to_be_visible()
    page.get_by_role("link", name="Next").click()
    expect(page).to_have_url(re.compile(r"page=2"))
    expect(showing(page, size + 1, 2 * size)).to_be_visible()
    page.go_back()
    assert_whole_page(page, "Audit")
    expect(page).not_to_have_url(re.compile(r"page=2"))
    expect(page.locator("#id_q")).to_have_value("material")
    expect(showing(page, 1, size)).to_be_visible()
    page.go_back()
    assert_whole_page(page, "Audit")
    expect(page).to_have_url(re.compile(rf"per_page={size}"))
    expect(page.locator("#id_q")).to_have_value("")
    expect(rows).to_have_count(size)
