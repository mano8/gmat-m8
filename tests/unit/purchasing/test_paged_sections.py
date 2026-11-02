"""The screens that rendered every row now page (M7d.3): the purchasing worklist (materials,
drafts and quantity ordered for nobody, each on its own parameter), a project's expected
deliveries and its materials. A screen never renders more than its page size of rows."""

import re
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse

from apps.catalog.models import Material, UnitOfMeasure
from apps.purchasing import selectors as purchasing_selectors
from apps.purchasing.models import PurchaseOrder
from apps.purchasing.selectors import UnallocatedRow
from tests.org import Org, People
from tests.org import client_for as signed_in
from tests.unit.purchasing.conftest import Buy, MakeLine, only_line

pytestmark = pytest.mark.django_db

SIZE = 10


def client_for(user: Any) -> Client:
    return signed_in(user, HTTP_ACCEPT_LANGUAGE="en")


def get(client: Client, name: str, *args: Any, **query: Any) -> str:
    response = client.get(reverse(name, args=args), query)
    assert response.status_code == 200
    return str(response.content.decode())


def link(html: str, rel: str) -> str:
    """The URL of the pager link with ``rel`` (``next`` or ``prev``), as the page writes it."""
    found = re.search(rf'href="([^"]*)"[^>]*rel="{rel}"', html)
    assert found, f"no {rel} link"
    return found[1].replace("&amp;", "&")


def follow(client: Client, html: str, rel: str) -> str:
    response = client.get(reverse("purchasing:worklist") + link(html, rel))
    assert response.status_code == 200
    return str(response.content.decode())


def materials(count: int, first: int = 0) -> list[Material]:
    metres = UnitOfMeasure.objects.get(code="M")
    return [
        Material.objects.create(
            reference=f"MAT-{number:03d}", name=f"Material {number}", unit=metres
        )
        for number in range(first, first + count)
    ]


class TestWorklistMaterials:
    def lines(self, make_line: MakeLine, count: int = SIZE + 1) -> list[Any]:
        return [make_line("3", material=material) for material in materials(count)]

    def test_pages_by_material_from_a_cursor_and_the_size_is_chosen(
        self, people: People, make_line: MakeLine
    ) -> None:
        self.lines(make_line)
        manager = client_for(people.mm_mad)
        first = get(manager, "purchasing:worklist", per_page=SIZE)
        assert first.count('data-testid="worklist-material"') == SIZE
        assert "10 shown" in first and 'rel="prev"' not in first
        assert "Ticks count for this page only" in first
        second = follow(manager, first, "next")
        assert second.count('data-testid="worklist-material"') == 1
        assert 'rel="next"' not in second  # the last page: nothing follows
        assert follow(manager, second, "prev").count('data-testid="worklist-material"') == SIZE
        everything = get(manager, "purchasing:worklist")
        assert everything.count('data-testid="worklist-material"') == SIZE + 1
        assert "Ticks count for this page only" not in everything

    def test_the_pages_walk_the_whole_backlog_once(
        self, people: People, make_line: MakeLine
    ) -> None:
        wanted = {line.pk for line in self.lines(make_line, 23)}
        manager = client_for(people.mm_mad)
        seen: list[str] = []
        html = get(manager, "purchasing:worklist", per_page=SIZE)
        while True:
            seen += re.findall(r'name="line"\s+value="([^"]+)"', html)
            if 'rel="next"' not in html:
                break
            html = follow(manager, html, "next")
        assert sorted(seen) == sorted(str(pk) for pk in wanted)

    def test_the_default_is_the_largest_page_that_stays_in_budget(
        self, people: People, make_line: MakeLine
    ) -> None:
        """25 materials a page, and no larger size is offered (100 cards did not fit 300 ms)."""
        assert purchasing_selectors.WORKLIST_PER_PAGE == 25
        self.lines(make_line, 26)
        manager = client_for(people.mm_mad)
        html = get(manager, "purchasing:worklist")
        assert html.count('data-testid="worklist-material"') == 25
        assert '<option value="50"' not in html and '<option value="25"' in html
        assert (
            get(manager, "purchasing:worklist", per_page=100).count(
                'data-testid="worklist-material"'
            )
            == 25
        )

    def test_a_line_ticked_on_page_two_starts_a_draft(
        self, org: Org, people: People, make_line: MakeLine
    ) -> None:
        lines = self.lines(make_line)
        manager = client_for(people.mm_mad)
        html = follow(manager, get(manager, "purchasing:worklist", per_page=SIZE), "next")
        (shown,) = [line for line in lines if f'value="{line.pk}"' in html]
        builder = get(manager, "purchasing:new", line=str(shown.pk))
        assert f'name="pick_{shown.pk}"' in builder and "checked" in builder
        data = {
            "idempotency_key": re.search(r'name="idempotency_key"[^>]*value="([^"]*)"', builder)[1],  # type: ignore[index]
            "items_key": re.search(r'name="items_key"[^>]*value="([^"]*)"', builder)[1],  # type: ignore[index]
            "branch": str(org.a_mad.pk),
            "supplier": str(org.supplier.pk),
            "ship_to": str(org.warehouses["A Madrid"].pk),
            f"pick_{shown.pk}": "on",
            f"qty_{shown.pk}": "3",
        }
        response = manager.post(reverse("purchasing:create"), data)
        assert response.status_code == 302
        assert PurchaseOrder.objects.filter(status=PurchaseOrder.Status.DRAFT).count() == 1

    def test_filters_keep_narrowing_the_scoped_lines(
        self, org: Org, people: People, make_line: MakeLine
    ) -> None:
        urgent = make_line("2", priority="URGENT", material=materials(1, first=500)[0])
        self.lines(make_line)
        html = get(client_for(people.mm_mad), "purchasing:worklist", priority="URGENT")
        assert html.count('data-testid="worklist-material"') == 1
        assert f'value="{urgent.pk}"' in html and "1 filter active" in html

    def test_bad_paging_parameters_fall_back(self, people: People, make_line: MakeLine) -> None:
        self.lines(make_line, 2)
        manager = client_for(people.mm_mad)
        for query in (
            {"after": "x"},
            {"after": "99.abcdefgh"},
            {"after": "1.nomatch"},
            {"back": ",,x"},
            {"per_page": "7"},
            {"drafts_page": "x"},
            {"unallocated_after": "-3"},
        ):
            assert get(manager, "purchasing:worklist", **query)

    def test_htmx_answers_the_results_with_the_sections(
        self, people: People, make_line: MakeLine
    ) -> None:
        self.lines(make_line, 2)
        response = client_for(people.mm_mad).get(
            reverse("purchasing:worklist"), headers={"HX-Request": "true"}
        )
        body = response.content.decode()
        assert 'id="worklist-results"' in body and "<html" not in body
        assert "HX-Request" in response.headers["Vary"]


class TestWorklistSections:
    def test_drafts_page_on_their_own_parameter(self, people: People, buy: Buy) -> None:
        for _ in range(SIZE + 1):
            buy(issue=False)
        manager = client_for(people.mm_mad)
        first = get(manager, "purchasing:worklist")
        assert first.count('data-testid="draft-order"') == SIZE
        assert 'href="?drafts_page=2#draft-orders"' in first
        second = get(manager, "purchasing:worklist", drafts_page=2)
        assert second.count('data-testid="draft-order"') == 1
        assert "drafts_per_page" not in second  # the size of a section is fixed

    def test_a_draft_page_does_not_move_the_materials(
        self, people: People, buy: Buy, make_line: MakeLine
    ) -> None:
        for material in materials(3):
            make_line("1", material=material)
            buy(issue=False)
        html = get(client_for(people.mm_mad), "purchasing:worklist", drafts_page=9)
        assert html.count('data-testid="worklist-material"') == 3
        assert html.count('data-testid="draft-order"') == 3

    def test_ordered_for_nobody_pages_on_its_own_cursor(
        self,
        monkeypatch: pytest.MonkeyPatch,
        people: People,
        buy: Buy,
        make_line: MakeLine,
    ) -> None:
        po_line = only_line(buy((make_line("10"), "10")))

        class Many:
            """SIZE + 1 order lines, each with four units on order for nobody."""

            def __init__(self, ctx: Any, **_: Any) -> None:
                self.keys = [f"line-{number:03d}" for number in range(SIZE + 1)]

            def window(self, chunk: Any) -> list[tuple[int, UnallocatedRow]]:
                return [(index, UnallocatedRow(po_line, Decimal(4))) for index in range(len(chunk))]

        monkeypatch.setattr(purchasing_selectors, "Unallocated", Many)
        manager = client_for(people.mm_mad)
        first = get(manager, "purchasing:worklist")
        assert first.count('data-testid="unallocated-line"') == SIZE
        assert "unallocated_after=" in link(first, "next")
        assert "#ordered-for-nobody" in link(first, "next")
        second = follow(manager, first, "next")
        assert second.count('data-testid="unallocated-line"') == 1
        assert "unallocated_per_page" not in second  # the size of a section is fixed

    def test_the_supplier_filter_still_narrows_the_drafts(
        self, org: Org, people: People, buy: Buy
    ) -> None:
        buy(issue=False)
        html = get(client_for(people.mm_mad), "purchasing:worklist", supplier=str(org.supplier.pk))
        assert html.count('data-testid="draft-order"') == 1 and "1 filter active" in html


class TestExpectedDeliveries:
    def orders(self, make_line: MakeLine, buy: Buy, count: int = SIZE + 1) -> list[PurchaseOrder]:
        return [buy((make_line("4"), "4")) for _ in range(count)]

    def test_pages_by_order(self, org: Org, people: People, make_line: MakeLine, buy: Buy) -> None:
        self.orders(make_line, buy)
        client = client_for(people.wo_mad)
        first = get(client, "receiving:expected", org.hospital.pk)
        assert first.count('data-testid="expected-order"') == SIZE
        assert "Showing 1\u201310 of 11" in first
        second = get(client, "receiving:expected", org.hospital.pk, page=2)
        assert second.count('data-testid="expected-order"') == 1
        wide = get(client, "receiving:expected", org.hospital.pk, per_page=25)
        assert wide.count('data-testid="expected-order"') == SIZE + 1

    def test_a_page_keeps_every_line_of_its_orders(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy
    ) -> None:
        both = buy((make_line("1"), "1"), (make_line("2"), "2"))
        html = get(client_for(people.wo_mad), "receiving:expected", org.hospital.pk)
        assert html.count('data-testid="expected-order"') == 1
        assert html.count('data-testid="expected-line"') == 2
        assert str(both.number) in html

    def test_filters_by_supplier_name_and_expected_date(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy
    ) -> None:
        soon, late, undated = self.orders(make_line, buy, 3)
        today = date.today()
        PurchaseOrder.objects.filter(pk=soon.pk).update(expected_date=today)
        PurchaseOrder.objects.filter(pk=late.pk).update(expected_date=today + timedelta(days=30))
        client = client_for(people.wo_mad)
        by_date = get(
            client,
            "receiving:expected",
            org.hospital.pk,
            expected_by=(today + timedelta(days=5)).isoformat(),
        )
        assert str(soon.number) in by_date
        assert str(late.number) not in by_date and str(undated.number) not in by_date
        assert "1 filter active" in by_date
        name = get(client, "receiving:expected", org.hospital.pk, supplier=org.supplier.name[:4])
        assert name.count('data-testid="expected-order"') == 3
        nobody = get(client, "receiving:expected", org.hospital.pk, supplier="no such supplier")
        assert "No expected delivery matches the filters." in nobody

    def test_no_price_or_contact_reaches_a_filtered_page(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy
    ) -> None:
        self.orders(make_line, buy, 2)
        html = get(client_for(people.wo_mad), "receiving:expected", org.hospital.pk, supplier="a")
        assert org.supplier.email not in html

    def test_another_projects_filtered_page_is_a_404(self, org: Org, people: People) -> None:
        response = client_for(people.site_manager).get(
            reverse("receiving:expected", args=[org.sev_for_mad.pk]), {"supplier": "x", "page": "2"}
        )
        assert response.status_code == 404

    def test_nothing_expected_still_says_so(self, org: Org, people: People) -> None:
        html = get(client_for(people.site_manager), "receiving:expected", org.hospital.pk)
        assert "Nothing is expected here" in html and "0 results" in html


class TestProjectMaterials:
    def lines(self, make_line: MakeLine, org: Org, count: int = SIZE + 1) -> None:
        for number, material in enumerate(materials(count)):
            make_line(str(number + 1), material=material, project=org.hospital)

    def test_pages_by_material(self, org: Org, people: People, make_line: MakeLine) -> None:
        self.lines(make_line, org)
        client = client_for(people.mm_mad)
        first = get(client, "requests:project_materials", org.hospital.pk, per_page=SIZE)
        assert first.count('data-testid="project-material"') == SIZE
        assert "Showing 1\u201310 of 11" in first
        second = get(client, "requests:project_materials", org.hospital.pk, per_page=SIZE, page=2)
        assert second.count('data-testid="project-material"') == 1
        default = get(client, "requests:project_materials", org.hospital.pk)
        assert default.count('data-testid="project-material"') == SIZE + 1

    def test_most_outstanding_stays_first(
        self, org: Org, people: People, make_line: MakeLine
    ) -> None:
        self.lines(make_line, org)
        html = get(client_for(people.mm_mad), "requests:project_materials", org.hospital.pk)
        assert html.index("MAT-010") < html.index("MAT-000")

    def test_text_filter_looks_at_name_and_reference(
        self, org: Org, people: People, make_line: MakeLine
    ) -> None:
        self.lines(make_line, org)
        client = client_for(people.mm_mad)
        by_name = get(client, "requests:project_materials", org.hospital.pk, q="material 7")
        assert by_name.count('data-testid="project-material"') == 1 and "MAT-007" in by_name
        by_reference = get(client, "requests:project_materials", org.hospital.pk, q="mat-003")
        assert "Material 3" in by_reference
        nothing = get(client, "requests:project_materials", org.hospital.pk, q="zzz")
        assert "Nothing is outstanding for this project." in nothing

    def test_outstanding_only_drops_what_is_settled(
        self,
        org: Org,
        people: People,
        make_line: MakeLine,
        delivered: dict[Any, Decimal],
    ) -> None:
        done, open_ = (
            make_line("5", project=org.hospital),
            make_line("5", project=org.hospital, material=materials(1, first=500)[0]),
        )
        delivered[done.pk] = Decimal("5")
        client = client_for(people.mm_mad)
        every = get(client, "requests:project_materials", org.hospital.pk)
        assert every.count('data-testid="project-material"') == 2
        only = get(client, "requests:project_materials", org.hospital.pk, outstanding="on")
        assert only.count('data-testid="project-material"') == 1
        assert (
            open_.material is not None and open_.material.name in only and "1 filter active" in only
        )

    def test_a_project_out_of_scope_is_a_404(self, org: Org, people: People) -> None:
        response = client_for(people.tech_mad).get(
            reverse("requests:project_materials", args=[org.sev_works.pk]), {"q": "x", "page": "2"}
        )
        assert response.status_code == 404

    def test_htmx_answers_the_results(self, org: Org, people: People, make_line: MakeLine) -> None:
        self.lines(make_line, org, 2)
        response = client_for(people.mm_mad).get(
            reverse("requests:project_materials", args=[org.hospital.pk]),
            headers={"HX-Request": "true"},
        )
        body = response.content.decode()
        assert 'id="materials-results"' in body and "<html" not in body
