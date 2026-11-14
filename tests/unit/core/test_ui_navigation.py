"""The signed-in navigation (M8b.2): categories per role, the phone bar and the current page."""

import re

import pytest
from django.conf import settings
from django.test import Client
from django.urls import reverse

from apps.core.navigation import CATEGORIES, PHONE_BAR_SIZE
from apps.core.preferences import CATEGORY_KEYS
from tests.org import Org, People, client_for

pytestmark = pytest.mark.django_db

ALL_GROUPS = ["work", "purchasing", "site", "organization", "insights", "administration"]
# Every category but Administration, which only ``org.manage`` reaches (M10.2).
BUSINESS_GROUPS = ALL_GROUPS[:-1]
SIDEBAR = re.compile(r'<nav id="app-menu".*?</nav>', re.S)
SHEET = re.compile(r'<div id="more-menu".*?\n    </div>', re.S)
BAR = re.compile(r'<nav class="fixed[^>]*>.*?</nav>', re.S)


def parts(html: str) -> tuple[str, str, str]:
    """The sidebar, the phone bar and the More sheet of a page."""
    sidebar = SIDEBAR.search(html)
    bar = BAR.search(html)
    assert sidebar and bar
    sheet = SHEET.search(html)
    return sidebar.group(0), bar.group(0), sheet.group(0) if sheet else ""


def groups_of(markup: str) -> list[str]:
    return re.findall(r'data-nav-group="(\w+)"', markup)


def links_of(markup: str) -> list[str]:
    return re.findall(r'<a href="([^"]+)"\s+class="(?:nav-link|bar-link)"', markup)


def page(client: Client, url: str = "/", **cookies: str) -> str:
    for name, value in cookies.items():
        client.cookies[name] = value
    response = client.get(url)
    assert response.status_code == 200
    return response.content.decode()


def _cookie_name() -> str:
    return settings.UI_COOKIE_NAME


def test_the_categories_are_the_ones_the_cookie_may_fold() -> None:
    assert tuple(category.key for category in CATEGORIES) == CATEGORY_KEYS


# What each role reaches: the capability of each entry decides, never the role's name.
# ``suppliers`` only for group admins, material managers, engineers and warehouse operators.
ROLES: list[tuple[str, list[str], list[str]]] = [
    (
        "group_admin",
        ALL_GROUPS,
        [
            "/org/people/",
            "/org/structure/companies/",
            "/org/structure/branches/",
            "/suppliers/",
            "/audit/",
            "/exports/",
            "/purchasing/",
        ],
    ),
    ("mm_mad", BUSINESS_GROUPS, ["/suppliers/", "/audit/", "/exports/", "/purchasing/"]),
    ("engineer", BUSINESS_GROUPS, ["/suppliers/", "/audit/", "/exports/", "/purchasing/"]),
    ("wo_mad", ["work", "purchasing", "site", "organization"], ["/suppliers/"]),
    # Q44 (M9.1): technicians and partners receive at their projects' worksites.
    (
        "tech_mad",
        ["work", "site", "organization"],
        ["/requests/", "/projects/", "/catalog/", "/receiving/expected/"],
    ),
    ("site_manager", ["work", "site", "organization"], ["/receiving/expected/"]),
    ("partner", ["work", "site", "organization"], ["/requests/", "/receiving/expected/"]),
    ("nobody", ["work"], ["/"]),
    ("operator", ["work"], ["/"]),
]


@pytest.mark.parametrize(("who", "groups", "has"), ROLES, ids=[row[0] for row in ROLES])
def test_a_category_shows_only_with_an_entry_the_role_reaches(
    org: Org, people: People, who: str, groups: list[str], has: list[str]
) -> None:
    sidebar, _bar, _sheet = parts(page(client_for(getattr(people, who))))
    assert groups_of(sidebar) == groups
    assert set(has) <= set(links_of(sidebar))


@pytest.mark.parametrize(
    "who",
    ["group_admin", "mm_mad", "wo_mad", "tech_mad", "site_manager", "partner", "nobody"],
)
def test_every_entry_shown_is_one_the_user_may_open(org: Org, people: People, who: str) -> None:
    """No entry without its capability: each link answers 200 for the user who sees it."""
    client = client_for(getattr(people, who))
    sidebar, _bar, _sheet = parts(page(client))
    for url in links_of(sidebar):
        response = client.get(url)
        assert response.status_code == 200, f"{who} sees {url} and gets {response.status_code}"


def test_supplier_entries_never_reach_technicians_partners_or_site_managers(
    org: Org, people: People
) -> None:
    for who in ("tech_mad", "partner", "site_manager"):
        html = page(client_for(getattr(people, who)))
        assert "/suppliers/" not in html
        assert "/audit/" not in html
        assert "/purchasing/" not in html


def test_the_bar_holds_four_entries_and_the_sheet_the_rest(org: Org, people: People) -> None:
    _sidebar, bar, sheet = parts(page(client_for(people.mm_mad)))
    on_bar = links_of(bar)
    assert len(on_bar) == PHONE_BAR_SIZE
    assert on_bar == ["/", "/requests/", "/requests/inbox/", "/purchasing/"]
    assert 'popovertarget="more-menu"' in bar
    rest = links_of(sheet)
    assert not set(rest) & set(on_bar)
    assert {"/purchasing/orders/", "/receiving/", "/suppliers/", "/audit/"} <= set(rest)


def test_a_user_with_four_entries_or_fewer_has_no_more_button(org: Org, people: People) -> None:
    _sidebar, bar, sheet = parts(page(client_for(people.nobody)))
    assert links_of(bar) == ["/"]
    assert "more-menu" not in bar
    assert sheet == ""


def test_a_technicians_bar_leads_to_receiving(org: Org, people: People) -> None:
    """Q44 (M9.1): receiving ranks before projects and the catalog; the rest is in More."""
    _sidebar, bar, sheet = parts(page(client_for(people.tech_mad)))
    assert links_of(bar)[:3] == ["/", "/requests/", "/receiving/"]
    assert "/catalog/" in links_of(sheet)


def test_the_phone_bar_order_follows_the_work_of_the_role(org: Org, people: People) -> None:
    _sidebar, bar, _sheet = parts(page(client_for(people.wo_mad)))
    assert links_of(bar)[:2] == ["/", "/requests/"]
    assert "/receiving/" in links_of(bar)


def test_the_current_page_is_marked_in_the_menu_and_the_bar(org: Org, people: People) -> None:
    html = page(client_for(people.mm_mad), "/purchasing/orders/")
    sidebar, bar, sheet = parts(html)
    assert re.search(
        r'href="/purchasing/orders/"\s+class="nav-link"\s+aria-current="page"', sidebar
    )
    assert 'aria-current="page"' not in bar  # Orders is in the More sheet, not on the bar
    assert re.search(r'href="/purchasing/orders/"\s+class="nav-link"\s+aria-current="page"', sheet)
    assert html.count('aria-current="page"') == 2  # the sidebar and the sheet


def test_the_top_bar_names_the_category_and_the_page(org: Org, people: People) -> None:
    html = page(client_for(people.mm_mad), "/receiving/expected/")
    assert re.search(r"Site &amp; warehouse|Site & warehouse|Obra y almac", html)
    assert "Entregas esperadas" in html or "Expected deliveries" in html


@pytest.mark.parametrize("cookie", ["", "f-work", "f-purchasing.f-insights", "hidden.f-site"])
def test_the_category_of_the_current_page_is_open_whatever_the_cookie_says(
    org: Org, people: People, cookie: str
) -> None:
    html = page(client_for(people.mm_mad), "/purchasing/orders/", **{_cookie_name(): cookie})
    sidebar, _bar, _sheet = parts(html)
    purchasing = re.search(r'<details[^>]*data-nav-group="purchasing"[^>]*>', sidebar)
    assert purchasing and re.search(r"\sopen(\s|>)", purchasing.group(0))


def test_the_other_categories_are_open_or_closed_as_the_person_left_them(
    org: Org, people: People
) -> None:
    html = page(client_for(people.mm_mad), "/", **{_cookie_name(): "f-purchasing.f-insights"})
    sidebar, _bar, _sheet = parts(html)
    state = {
        key: bool(re.search(rf'<details[^>]*data-nav-group="{key}"[^>]*\sopen(\s|>)', sidebar))
        for key in BUSINESS_GROUPS
    }
    assert state == {
        "work": True,
        "purchasing": False,
        "site": True,
        "organization": True,
        "insights": False,
    }
    assert 'data-saved="closed"' in sidebar


def test_the_menu_is_open_by_default_and_every_category_too(org: Org, people: People) -> None:
    sidebar, _bar, _sheet = parts(page(client_for(people.mm_mad)))
    assert " hidden" not in sidebar.split(">", 1)[0]
    assert sidebar.count("<details") == sidebar.count(" open") >= 5


def test_a_hidden_menu_carries_the_hidden_attribute_and_the_button_says_show(
    org: Org, people: People
) -> None:
    html = page(client_for(people.mm_mad), **{_cookie_name(): "hidden"})
    sidebar, _bar, _sheet = parts(html)
    assert re.match(r"<nav id=\"app-menu\"[^>]*\shidden>", sidebar)
    toggle = re.search(r"<button[^>]*data-menu-toggle[^>]*>", html)
    assert toggle
    assert 'aria-expanded="false"' in toggle.group(0)
    assert 'aria-controls="app-menu"' in toggle.group(0)
    assert 'value="shown"' in toggle.group(0)
    assert "Mostrar el menú" in html or "Show the menu" in html


def test_the_menu_button_is_a_form_that_posts_to_the_preferences(org: Org, people: People) -> None:
    html = page(client_for(people.mm_mad))
    form = re.search(r'<form action="/ui/preferences/"[^>]*>.*?data-menu-toggle', html, re.S)
    assert form
    assert 'method="post"' in form.group(0)
    assert "csrfmiddlewaretoken" in form.group(0)
    assert 'name="menu"' in form.group(0)


def test_the_user_menu_holds_the_account_links_the_language_the_theme_and_sign_out(
    org: Org, people: People
) -> None:
    html = page(client_for(people.tech_mad))
    menu = re.search(r'<div id="user-menu".*?\n    </div>', html, re.S)
    assert menu
    markup = menu.group(0)
    assert people.tech_mad.email in markup
    for target in ("/account/sessions/", "/account/notifications/", reverse("mfa_index")):
        assert f'href="{target}"' in markup
    assert 'id="language"' in markup
    assert markup.count('name="theme"') == 3
    assert 'action="/accounts/logout/"' in markup
    assert 'href="/accounts/logout/"' not in markup


def test_the_quick_action_is_there_only_with_request_create(org: Org, people: People) -> None:
    assert 'href="/requests/new/"' in page(client_for(people.tech_mad))
    assert 'href="/requests/new/"' not in page(client_for(people.nobody))
