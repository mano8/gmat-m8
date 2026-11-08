"""The home page and the panel fragments (plan M7.2)."""

import time
from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.access.context import AccessContext
from apps.accounts.models import User
from apps.dashboards import panels
from apps.purchasing.models import PurchaseOrder
from tests.org import Org, People, Sentinel, build_sentinel_order, client_for
from tests.support import enroll_totp
from tests.unit.dashboards.conftest import Requests
from tests.unit.purchasing.conftest import MakeLine

pytestmark = pytest.mark.django_db

PANEL_BUDGET = 0.3  # seconds per panel with the demo data (plan M7.2)


def panel_url(key: str) -> str:
    return reverse("dashboard:panel", kwargs={"key": key})


def placeholders(html: str) -> set[str]:
    return {key for key in panels.BY_KEY if f'data-testid="panel-{key}"' in html}


class TestHome:
    def test_a_technician_gets_the_own_requests_panel_lazily(self, people: People) -> None:
        response = client_for(people.tech_mad, mfa=False).get(reverse("core:home"))
        html = response.content.decode()
        assert response.status_code == 200
        assert placeholders(html) == {"my-requests", "request-progress"}
        assert f'hx-get="{panel_url("my-requests")}"' in html
        assert 'hx-trigger="load"' in html

    def test_a_group_administrator_gets_every_panel(self, people: People) -> None:
        html = client_for(people.group_admin).get(reverse("core:home")).content.decode()
        assert placeholders(html) == set(panels.BY_KEY)

    def test_an_account_without_membership_gets_the_empty_state(self, people: People) -> None:
        response = client_for(people.nobody, mfa=False).get(reverse("core:home"))
        assert response.status_code == 200
        assert placeholders(response.content.decode()) == set()


class TestPanel:
    def test_the_assigned_panel_links_to_the_filtered_inbox(self, people: People) -> None:
        html = client_for(people.engineer).get(panel_url("assigned")).content.decode()
        assert f'href="{reverse("requests:inbox")}?assigned=1"' in html

    def test_the_fragment_has_the_count_the_rows_and_the_link(
        self, people: People, make_line: MakeLine
    ) -> None:
        line = make_line("4")
        response = client_for(people.tech_mad, mfa=False).get(panel_url("my-requests"))
        html = response.content.decode()
        assert response.status_code == 200
        assert "<html" not in html  # a fragment, not a page
        assert (line.request.number or "") in html
        assert reverse("requests:detail", args=[line.request_id]) in html
        assert f'href="{reverse("requests:list")}"' in html
        assert "no-store" in response["Cache-Control"]

    def test_a_capped_count_reads_with_a_plus(
        self,
        monkeypatch: pytest.MonkeyPatch,
        org: Org,
        people: People,
        make_line: MakeLine,
    ) -> None:
        monkeypatch.setattr(panels, "COUNT_CAP", 2)
        for _ in range(3):
            make_line("4")
        html = client_for(people.mm_mad).get(reverse("dashboard:panel", args=["to-purchase"]))
        assert ">2+<" in html.content.decode().replace(" ", "").replace("\n", "")

    def test_an_empty_panel_says_so(self, people: People) -> None:
        html = client_for(people.mm_mad).get(panel_url("to-purchase")).content.decode()
        assert str(panels.BY_KEY["to-purchase"].empty) in html

    def test_a_panel_needs_its_capability(self, people: People) -> None:
        assert (
            client_for(people.tech_mad, mfa=False).get(panel_url("to-purchase")).status_code == 403
        )
        assert client_for(people.partner, mfa=False).get(panel_url("overview")).status_code == 403

    def test_an_unknown_panel_and_a_signed_out_visitor(self, people: People) -> None:
        assert client_for(people.mm_mad).get(panel_url("nothing")).status_code == 404
        response = Client().get(panel_url("my-requests"))
        assert response.status_code == 302 and "login" in response["Location"]

    def test_only_get_reaches_a_panel(self, people: People) -> None:
        assert client_for(people.mm_mad).post(panel_url("to-purchase")).status_code == 405

    def test_a_partner_sees_no_supplier_or_foreign_request(
        self, org: Org, people: People, make_line: MakeLine
    ) -> None:
        make_line("4")  # the technician's request, not the partner's
        html = client_for(people.partner, mfa=False).get(panel_url("my-requests")).content.decode()
        assert "SOL-T-" not in html
        assert org.supplier.name not in html


@pytest.mark.serial  # a wall-clock budget: never under pytest-xdist
def test_every_panel_answers_within_budget_with_the_demo_data(group_admin: User) -> None:
    call_command("seed_demo", "--operator", group_admin.email, stdout=StringIO())
    enroll_totp(group_admin)
    client = client_for(group_admin)
    for key in panels.BY_KEY:
        started = time.perf_counter()
        response = client.get(panel_url(key))
        elapsed = time.perf_counter() - started
        assert response.status_code == 200, key
        assert elapsed < PANEL_BUDGET, f"{key}: {elapsed:.3f}s"


# --- M8b.5: key figures, charts and the period -------------------------------------------


def tiles_url(weeks: str = "") -> str:
    return reverse("dashboard:tiles") + (f"?weeks={weeks}" if weeks else "")


CHART_SCRIPT = "vendor/chartjs/chart.umd"


class TestPeriodAndCharts:
    def test_the_home_page_offers_the_period_and_loads_the_chart_library(
        self, people: People
    ) -> None:
        html = client_for(people.mm_mad).get(reverse("core:home"), {"weeks": "4"}).content.decode()
        assert CHART_SCRIPT in html and "js/charts.js" in html
        assert '<a href="?weeks=4"\n                 aria-current="true">' in html or (
            'href="?weeks=4"' in html and 'aria-current="true"' in html
        )
        assert f'hx-get="{panel_url("decisions")}?weeks=4"' in html
        assert f'hx-get="{tiles_url("4")}"' in html

    @pytest.mark.parametrize(
        "weeks", ["7", "abc", "-12", ""], ids=["7", "text", "negative", "none"]
    )
    def test_a_period_outside_the_allowlist_falls_back_to_twelve(
        self, people: People, requests: Requests, weeks: str
    ) -> None:
        requests.make(status="APPROVED", decided=timezone.now(), lines=("APPROVED",))
        client = client_for(people.mm_mad)
        html = client.get(reverse("core:home"), {"weeks": weeks}).content.decode()
        assert f'hx-get="{panel_url("decisions")}?weeks=12"' in html
        chart = client.get(panel_url("decisions"), {"weeks": weeks}).content.decode()
        assert chart.count('<th scope="row">') == 12

    def test_a_requester_without_a_chart_gets_no_chart_library(self, people: People) -> None:
        html = client_for(people.tech_mad, mfa=False).get(reverse("core:home")).content.decode()
        assert CHART_SCRIPT not in html
        assert 'data-testid="period"' not in html
        assert 'data-testid="key-figures"' in html

    def test_an_account_without_a_role_gets_no_figures(self, people: People) -> None:
        client = client_for(people.nobody, mfa=False)
        html = client.get(reverse("core:home")).content.decode()
        assert 'data-testid="key-figures"' not in html
        response = client.get(tiles_url())
        assert response.status_code == 200
        assert 'data-testid="tile-' not in response.content.decode()

    def test_the_key_figures_fragment(self, people: People) -> None:
        client = client_for(people.mm_mad)
        html = client.get(tiles_url("26")).content.decode()
        for key in ("inbox", "decision-time", "to-purchase", "overdue"):
            assert f'data-testid="tile-{key}"' in html
        assert client.post(tiles_url()).status_code == 405
        response = Client().get(tiles_url())
        assert response.status_code == 302 and "login" in response["Location"]

    def test_a_chart_fragment_carries_its_data_as_an_attribute_and_a_table(
        self, people: People, requests: Requests
    ) -> None:
        requests.make(status="APPROVED", decided=timezone.now(), lines=("APPROVED",))
        html = (
            client_for(people.mm_mad).get(panel_url("decisions"), {"weeks": "4"}).content.decode()
        )
        assert '<canvas data-chart="{&quot;type&quot;:&quot;bar&quot;' in html
        assert 'role="img"' in html and 'aria-label="' in html
        assert "<script" not in html  # htmx would strip it: the data is an attribute
        assert html.count('<th scope="row">') == 4
        assert "data-chart-table" in html and " open " in html

    def test_a_meters_panel_draws_meters(self, people: People, make_line: MakeLine) -> None:
        make_line("4")
        html = (
            client_for(people.tech_mad, mfa=False)
            .get(panel_url("request-progress"))
            .content.decode()
        )
        assert "<meter" in html and "data-chart=" not in html

    def test_a_chart_needs_every_capability_it_reads_with(self, people: People) -> None:
        assert client_for(people.mm_mad).get(panel_url("branch-requests")).status_code == 403
        assert client_for(people.wo_mad).get(panel_url("request-progress")).status_code == 403
        assert client_for(people.group_admin).get(panel_url("branch-people")).status_code == 200

    def test_the_handover_ranking_is_for_who_hands_over(self, people: People) -> None:
        """Like the "to hand over" list: a site manager reads handovers, not the stock waiting
        to be handed over (plan §3.4)."""
        assert (
            client_for(people.site_manager).get(panel_url("handover-projects")).status_code == 403
        )


class TestChartLeaks:
    """Per projection (plan §8.2): a chart never carries a price or another project's
    quantity, and a supplier only to ``supplier.view`` holders."""

    @pytest.mark.parametrize(
        ("actor", "allowed"),
        [
            ("tech_mad", frozenset()),
            ("partner", frozenset()),
            ("site_manager", frozenset()),
            ("wo_mad", frozenset({Sentinel.SUPPLIER_NAME})),
            ("mm_mad", frozenset({Sentinel.SUPPLIER_NAME})),
            ("group_admin", frozenset({Sentinel.SUPPLIER_NAME})),
        ],
        ids=["technician", "partner", "site-manager", "warehouse", "manager", "admin"],
    )
    def test_no_sentinel_outside_the_projection(
        self, org: Org, people: People, actor: str, allowed: frozenset[Sentinel]
    ) -> None:
        sentinel = build_sentinel_order(org, people)
        PurchaseOrder.objects.filter(pk=sentinel.order.pk).update(
            expected_date=timezone.localdate() - timedelta(days=5)
        )
        user = getattr(people, actor)
        client = client_for(user, mfa=actor in {"wo_mad", "mm_mad", "group_admin"})
        ctx = AccessContext.for_user(user)
        pages = [tiles_url()] + [
            panel_url(panel.key) for panel in panels.visible(ctx) if panel.kind != "list"
        ]
        for url in pages:
            html = client.get(url).content.decode()
            for found in Sentinel:
                if found not in allowed:
                    assert not found.found_in(html), (actor, url, found)
        if Sentinel.SUPPLIER_NAME in allowed:
            html = client.get(panel_url("overdue-suppliers")).content.decode()
            assert Sentinel.SUPPLIER_NAME.found_in(html)
