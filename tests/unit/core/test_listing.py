"""The shared list toolkit (M7d.1): parameters, tie-breaker, htmx answers and components."""

import re
from typing import Any

import pytest
from django import forms
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ImproperlyConfigured
from django.http import HttpResponse
from django.template.loader import get_template
from django.test import RequestFactory
from django.urls import reverse
from django.utils import translation

from apps.catalog.models import Material, UnitOfMeasure
from apps.core import listing
from apps.core.http import is_htmx
from apps.core.listing import PAGE_SIZES, ListPage, ListScope, paginate, render_list
from apps.core.templatetags.list_tags import active_filter_count, clear_filters_url, page_url
from tests.org import Org, People, client_for

SORTS = {"code": ("code",), "name": ("name",)}


def get(query: str = "") -> Any:
    return RequestFactory().get(f"/list/?{query}")


def ordering(page: ListPage) -> tuple[Any, ...]:
    return tuple(page.page.object_list.query.order_by)  # type: ignore[attr-defined]


def numbers(page: ListPage) -> list[int]:
    return list(page.rows)


class TestParams:
    def test_the_page_sizes_are_the_four_of_q39(self) -> None:
        assert PAGE_SIZES == (10, 25, 50, 100)

    def test_defaults(self) -> None:
        page = paginate(get(), list(range(100)), default_per_page=25)
        assert (page.number, page.per_page, page.sort) == (1, 25, "")
        assert numbers(page) == list(range(25))

    @pytest.mark.parametrize("size", PAGE_SIZES)
    def test_every_size_is_accepted(self, size: int) -> None:
        page = paginate(get(f"per_page={size}"), list(range(300)), default_per_page=25)
        assert (page.per_page, len(page.rows)) == (size, size)

    @pytest.mark.parametrize("value", ["7", "10000", "-10", "abc", "", "25.5", "0"])
    def test_an_invalid_size_falls_back_to_the_default(self, value: str) -> None:
        page = paginate(get(f"per_page={value}"), list(range(300)), default_per_page=50)
        assert page.per_page == 50

    @pytest.mark.parametrize("value", ["0", "-1", "abc", "", "1.5", " "])
    def test_an_invalid_page_is_the_first(self, value: str) -> None:
        page = paginate(get(f"page={value}"), list(range(100)), default_per_page=10)
        assert page.number == 1

    @pytest.mark.parametrize("value", ["999", str(10**30)])
    def test_a_page_past_the_end_is_the_last(self, value: str) -> None:
        page = paginate(get(f"page={value}"), list(range(95)), default_per_page=10)
        assert (page.number, numbers(page)) == (10, [90, 91, 92, 93, 94])

    def test_a_valid_value_survives_an_invalid_one(self) -> None:
        page = paginate(get("page=3&per_page=7"), list(range(100)), default_per_page=10)
        assert (page.number, page.per_page) == (3, 10)

    def test_a_default_that_is_not_a_size_is_a_programming_error(self) -> None:
        with pytest.raises(ImproperlyConfigured):
            paginate(get(), [], default_per_page=12)

    def test_range_and_total(self) -> None:
        page = paginate(get("page=2"), list(range(312)), default_per_page=25)
        assert (page.first, page.last, page.total, page.num_pages) == (26, 50, 312, 13)

    def test_an_empty_list(self) -> None:
        page = paginate(get(), [], default_per_page=10)
        assert (page.first, page.last, page.total, page.has_other_pages) == (0, 0, 0, False)

    def test_the_window_has_gaps(self) -> None:
        page = paginate(get("page=6"), list(range(200)), default_per_page=10)
        assert page.pages == [1, None, 5, 6, 7, None, 20]
        first = paginate(get(), list(range(200)), default_per_page=10)
        assert first.pages == [1, 2, None, 20]


@pytest.mark.django_db
class TestQuerysets:
    def codes(self, page: ListPage) -> list[str]:
        return [unit.code for unit in page.rows]

    def test_the_primary_key_follows_the_querysets_own_order(self) -> None:
        page = paginate(get(), UnitOfMeasure.objects.order_by("name"), default_per_page=10)
        assert ordering(page) == ("name", "pk")

    def test_the_models_default_order_gets_it_too(self) -> None:
        page = paginate(get(), UnitOfMeasure.objects.all(), default_per_page=10)
        assert ordering(page) == ("code", "pk")

    def test_a_queryset_without_an_order_is_refused(self) -> None:
        with pytest.raises(ImproperlyConfigured):
            paginate(get(), UnitOfMeasure.objects.order_by(), default_per_page=10)

    def test_a_sort_replaces_the_order_and_keeps_the_tie_breaker(self) -> None:
        page = paginate(
            get("sort=name"), UnitOfMeasure.objects.all(), default_per_page=10, sorts=SORTS
        )
        assert page.sort == "name"
        assert ordering(page) == ("name", "pk")

    def test_a_descending_sort(self) -> None:
        page = paginate(
            get("sort=-code"), UnitOfMeasure.objects.all(), default_per_page=10, sorts=SORTS
        )
        assert page.sort == "-code"
        assert ordering(page) == ("-code", "pk")
        assert self.codes(page) == sorted(self.codes(page), reverse=True)

    @pytest.mark.parametrize("value", ["id", "-id", "name,code", "password", "--code", ""])
    def test_an_unknown_sort_is_the_default_order(self, value: str) -> None:
        page = paginate(
            get(f"sort={value}"), UnitOfMeasure.objects.all(), default_per_page=10, sorts=SORTS
        )
        assert page.sort == ""
        assert ordering(page) == ("code", "pk")

    def test_the_default_sort_applies_and_names_the_column(self) -> None:
        page = paginate(
            get("sort=nope"),
            UnitOfMeasure.objects.all(),
            default_per_page=10,
            sorts=SORTS,
            default_sort="code",
        )
        assert page.sort == "code"

    def test_a_sort_on_a_plain_sequence_is_ignored(self) -> None:
        page = paginate(get("sort=name"), [3, 1, 2], default_per_page=10, sorts=SORTS)
        assert numbers(page) == [3, 1, 2]

    def test_pages_do_not_overlap_or_skip_with_equal_values(self) -> None:
        seen: list[Any] = []
        total = UnitOfMeasure.objects.count()
        for number in range(1, total // 10 + 2):
            page = paginate(
                get(f"page={number}&sort=decimals"),
                UnitOfMeasure.objects.all(),
                default_per_page=10,
                sorts={"decimals": ("decimals",)},
            )
            seen.extend(unit.pk for unit in page.rows)
        assert len(seen) == len(set(seen)) == total

    def test_sort_options_for_the_phone_select(self) -> None:
        with translation.override("en"):
            page = paginate(
                get(),
                UnitOfMeasure.objects.all(),
                default_per_page=10,
                sorts=SORTS,
                sort_labels={"code": "Code"},
            )
        assert page.sort_options == (
            ("", "Default order"),
            ("code", "Code (ascending)"),
            ("-code", "Code (descending)"),
        )


class Filters(forms.Form):
    q = forms.CharField(required=False)
    state = forms.CharField(required=False)


class TestTags:
    def test_counts_the_filters_in_force(self) -> None:
        assert active_filter_count(Filters({"q": "cable", "state": ""})) == 1
        assert active_filter_count(Filters({"q": "cable", "state": "all"})) == 2
        assert active_filter_count(Filters()) == 0
        assert active_filter_count(None) == 0

    def test_clearing_keeps_the_page_size_and_sort(self) -> None:
        request = get("q=a&state=all&page=3&per_page=50&sort=-name&other=x")
        url = clear_filters_url({"request": request}, Filters(request.GET))
        assert url == "/list/?per_page=50&sort=-name&other=x"

    def test_clearing_everything_is_the_bare_path(self) -> None:
        request = get("q=a&page=2")
        assert clear_filters_url({"request": request}, Filters(request.GET)) == "/list/"


def render(name: str, request: Any, **context: Any) -> str:
    request.user = AnonymousUser()
    return get_template(name).render(context, request)


class TestComponents:
    def pager(self, query: str, total: int = 312, per_page: int = 25) -> str:
        request = get(query)
        page = paginate(request, list(range(total)), default_per_page=25)
        with translation.override("en"):
            return render("components/pagination.html", request, listing=page)

    def test_a_middle_page_has_every_control(self) -> None:
        html = self.pager("page=6&q=cable&per_page=25")
        assert "Showing 126\u2013150 of 312" in html
        assert 'aria-current="page"' in html and ">6</span>" in html
        assert 'rel="prev"' in html and 'rel="next"' in html
        assert 'aria-label="First page"' in html and 'aria-label="Last page"' in html
        assert "Page 6 of 13" in html
        assert "…" in html
        # filters and size are kept, the number replaced
        links = re.findall(r'href="([^"]+)"', html)
        assert links and all("q=cable" in link and "per_page=25" in link for link in links)
        assert "?page=7&amp;q=cable&amp;per_page=25" in html

    def test_the_first_page_disables_previous_and_first(self) -> None:
        html = self.pager("")
        assert 'rel="prev"' not in html and 'rel="next"' in html
        assert html.count('aria-disabled="true"') == 2

    def test_the_last_page_disables_next_and_last(self) -> None:
        html = self.pager("page=13")
        assert 'rel="next"' not in html and 'rel="prev"' in html
        assert html.count('aria-disabled="true"') == 2
        assert "Showing 301\u2013312 of 312" in html

    def test_one_page_has_no_navigation(self) -> None:
        html = self.pager("", total=7)
        assert "<nav" not in html
        assert "Showing 1\u20137 of 7" in html

    def test_an_empty_list_shows_nothing(self) -> None:
        assert "Showing" not in self.pager("", total=0) and "<nav" not in self.pager("", total=0)

    def test_the_summary_counts_and_clears(self) -> None:
        request = get("q=a&state=all&per_page=50&page=2")
        page = paginate(request, list(range(3)), default_per_page=25)
        with translation.override("en"):
            html = render(
                "components/list_summary.html", request, form=Filters(request.GET), listing=page
            )
        assert "3 results" in html and "2 filters active" in html
        assert 'href="/list/?per_page=50"' in html
        one = paginate(get(), [1], default_per_page=25)
        with translation.override("en"):
            html = render(
                "components/list_summary.html", get(), form=Filters(get().GET), listing=one
            )
        assert "1 result" in html and "Clear filters" not in html

    def test_the_sort_header_toggles(self) -> None:
        request = get("page=2&q=a")
        names = {"name": ("name",)}
        with translation.override("en"):
            none = render(
                "components/sort_header.html",
                request,
                listing=paginate(request, [1], default_per_page=10, sorts=names),
                key="name",
                label="Name",
            )
            up_request = get("sort=name&page=2")
            up = render(
                "components/sort_header.html",
                up_request,
                listing=paginate(up_request, [1], default_per_page=10, sorts=names),
                key="name",
                label="Name",
            )
            down_request = get("sort=-name")
            down = render(
                "components/sort_header.html",
                down_request,
                listing=paginate(down_request, [1], default_per_page=10, sorts=names),
                key="name",
                label="Name",
            )
        assert "aria-sort" not in none and "?page=2&amp;q=a&amp;sort=name" not in none
        assert 'href="?q=a&amp;sort=name"' in none
        assert 'aria-sort="ascending"' in up and 'href="?sort=-name"' in up
        assert 'aria-sort="descending"' in down and 'href="?sort=name"' in down


@pytest.mark.django_db
class TestCatalogReference:
    """The catalog list runs on the toolkit."""

    @pytest.fixture(autouse=True)
    def materials(self, org: Org) -> None:
        unit = UnitOfMeasure.objects.get(code="UD")
        for number in range(3):
            Material.objects.create(reference=f"M-{number}", name=f"Item {number}", unit=unit)

    def test_the_toolbar_summary_and_pager_render(self, org: Org, people: People) -> None:
        client = client_for(people.group_admin)
        with translation.override("en"):
            response = client.get(reverse("catalog:list"), {"per_page": "10", "sort": "-name"})
        html = response.content.decode()
        assert response.status_code == 200
        assert 'name="per_page"' in html and 'form="list-filter"' in html
        assert 'aria-sort="descending"' in html
        assert "data-list-status" in html

    def test_a_bad_parameter_is_never_an_error(self, org: Org, people: People) -> None:
        client = client_for(people.group_admin)
        response = client.get(
            reverse("catalog:list"), {"page": "x", "per_page": "10000", "sort": "password"}
        )
        assert response.status_code == 200

    def test_htmx_gets_the_fragment_and_a_vary_header(self, org: Org, people: People) -> None:
        client = client_for(people.group_admin)
        response = client.get(reverse("catalog:list"), headers={"HX-Request": "true"})
        assert b"<html" not in response.content and b'id="material-results"' in response.content
        assert "HX-Request" in response["Vary"]
        full = client.get(reverse("catalog:list"))
        assert b"<html" in full.content and "HX-Request" in full["Vary"]

    def test_a_history_restore_gets_the_whole_page(self, org: Org, people: People) -> None:
        client = client_for(people.group_admin)
        response = client.get(
            reverse("catalog:list"),
            {"q": "x"},
            headers={"HX-Request": "true", "HX-History-Restore-Request": "true"},
        )
        assert b"<html" in response.content


class TestIsHtmx:
    def test_without_the_middleware_it_is_false(self) -> None:
        assert is_htmx(RequestFactory().get("/")) is False

    def test_render_list_picks_the_partial(self, monkeypatch: pytest.MonkeyPatch) -> None:
        names: list[str] = []

        def fake_render(request: Any, name: str, context: Any) -> HttpResponse:
            names.append(name)
            return HttpResponse()

        monkeypatch.setattr(listing, "render", fake_render)
        request = RequestFactory().get("/")
        render_list(request, "x/list.html", {})
        request.htmx = True  # type: ignore[attr-defined]
        render_list(request, "x/list.html", {})
        assert names == ["x/list.html", "x/list.html#results"]


class SectionFilters(forms.Form):
    state = forms.CharField(required=False)


DRAFTS = ListScope(prefix="drafts_", anchor="draft-orders", sized=False)
ROLES = ListScope(prefix="roles_", anchor="roles")


class TestScopes:
    """A page with several lists: each reads and links its own parameters (M7d.3)."""

    def test_the_parameters_are_named_after_the_prefix(self) -> None:
        assert (ROLES.page_param, ROLES.per_page_param, ROLES.sort_param) == (
            "roles_page",
            "roles_per_page",
            "roles_sort",
        )
        whole = paginate(get(), [1], default_per_page=10)
        assert (whole.page_param, whole.per_page_param, whole.sort_param) == (
            "page",
            "per_page",
            "sort",
        )

    def test_a_section_ignores_the_parameters_of_the_page_and_of_other_sections(self) -> None:
        rows = list(range(300))
        request = get("page=3&per_page=50&roles_page=2&roles_per_page=10&sort=name")
        roles = paginate(request, rows, default_per_page=25, scope=ROLES)
        assert (roles.number, roles.per_page) == (2, 10)
        whole = paginate(request, rows, default_per_page=25)
        assert (whole.number, whole.per_page) == (3, 50)
        other = paginate(request, rows, default_per_page=25, scope=ListScope(prefix="x_"))
        assert (other.number, other.per_page) == (1, 25)

    def test_a_section_sorts_by_its_own_parameter(self) -> None:
        sorted_ = paginate(
            get("roles_sort=-name&sort=code"),
            Material.objects.none(),
            default_per_page=10,
            sorts=SORTS,
            scope=ROLES,
        )
        assert sorted_.sort == "-name"

    def test_a_fixed_size_section_never_follows_a_per_page(self) -> None:
        page = paginate(
            get("drafts_per_page=100&drafts_page=2"),
            list(range(30)),
            default_per_page=10,
            scope=DRAFTS,
        )
        assert (page.per_page, page.number, page.sizes) == (10, 2, ())
        assert paginate(get(), [1], default_per_page=10, scope=ROLES).sizes == PAGE_SIZES

    def test_the_page_link_changes_only_its_own_page(self) -> None:
        request = get("q=a&page=2&roles_page=3")
        roles = paginate(request, list(range(100)), default_per_page=10, scope=ROLES)
        whole = paginate(request, list(range(100)), default_per_page=10)
        assert page_url({"request": request}, roles, 4) == "?q=a&page=2&roles_page=4#roles"
        assert page_url({"request": request}, whole, 5) == "?q=a&page=5&roles_page=3"

    def test_clearing_a_section_keeps_the_rest_and_returns_to_it(self) -> None:
        request = get("roles-state=revoked&roles_page=2&roles_per_page=50&projects_page=3")
        roles = paginate(request, list(range(100)), default_per_page=10, scope=ROLES)
        form = SectionFilters(request.GET, prefix="roles")
        url = clear_filters_url({"request": request}, form, roles)
        assert url == "/list/?roles_per_page=50&projects_page=3#roles"
        assert active_filter_count(form) == 1
        assert active_filter_count(SectionFilters({"state": "x"}, prefix="roles")) == 0

    def test_the_toolbar_of_a_section_is_scoped_and_names_its_parameters(self) -> None:
        request = get("")
        roles = paginate(request, list(range(50)), default_per_page=10, scope=ROLES)
        with translation.override("en"):
            html = render(
                "components/list_toolbar.html",
                request,
                form=SectionFilters(prefix="roles"),
                listing=roles,
                form_id="roles-filter",
                scoped=True,
                select="#roles-results",
                target="#roles-results",
                action="/list/",
            )
        assert '<div class="list-scope">' in html
        assert 'id="roles-filter"' in html and 'hx-select="#roles-results"' in html
        assert "from:closest .list-scope" in html
        assert 'name="roles_per_page"' in html and 'form="roles-filter"' in html
        fixed = paginate(request, list(range(50)), default_per_page=10, scope=DRAFTS)
        with translation.override("en"):
            html = render(
                "components/list_toolbar.html",
                request,
                form=SectionFilters(),
                listing=fixed,
                target="#x",
                action="/list/",
            )
        assert "Rows per page" not in html and "list-scope" not in html
        assert "hx-select" not in html and "from:input" in html


@pytest.mark.django_db
class TestRevocation:
    def test_the_state_filter_and_the_open_first_order(self, org: Org, people: People) -> None:
        from django.utils import timezone

        from apps.access.roles import Role
        from apps.organizations.models import Membership
        from tests.support import grant

        grant(people.tech_mad, Role.TECHNICIAN, branch=org.a_sev, revoked_at=timezone.now())
        rows = Membership.objects.filter(user=people.tech_mad)
        revoked = listing.by_revocation(rows, listing.REVOKED)
        active = listing.by_revocation(rows, listing.ACTIVE)
        assert (revoked.count(), active.count()) == (1, 1)
        assert listing.by_revocation(rows, "").count() == 2
        assert listing.by_revocation(rows, "nope").count() == 2
        ordered = rows.order_by(listing.OPEN_FIRST, "-valid_from")
        assert [row.revoked_at is None for row in ordered] == [True, False]
