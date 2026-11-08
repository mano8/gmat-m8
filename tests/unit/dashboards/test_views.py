"""The home page and the panel fragments (plan M7.2)."""

import time
from io import StringIO

import pytest
from django.core.management import call_command
from django.test import Client
from django.urls import reverse

from apps.accounts.models import User
from apps.dashboards import panels
from tests.org import Org, People, client_for
from tests.support import enroll_totp
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
        assert placeholders(html) == {"my-requests"}
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
