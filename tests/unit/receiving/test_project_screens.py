"""The project receiving screens for every role (M9.2, Q44, ADR-0028).

Each of the seven business roles finds "Receive materials" on the page of a project in its
scope, reads the project's expected deliveries, posts the receipt (a double tap makes one) and
reads it back, with no purchasing capability to start with. Who has only the site reach
(site managers, technicians, partners) is never offered the order search, and never sees a
price, the supplier's contact details, another project's quantity, the whole order or a
handover document.
"""

import re
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse

from apps.organizations.models import Branch
from apps.projects.models import Project
from apps.receiving.models import GoodsReceipt
from tests.org import Org, People, Sentinel, build_sentinel_order
from tests.org import client_for as signed_in
from tests.unit.purchasing.conftest import Buy, MakeLine
from tests.unit.receiving.conftest import Receive
from tests.unit.receiving.test_views import page, receipt_payload

pytestmark = pytest.mark.django_db

ROLES = [
    "group_admin",
    "mm_mad",
    "engineer",
    "wo_mad",
    "site_manager",
    "tech_mad",
    "partner",
]
SITE_ONLY = ["site_manager", "tech_mad", "partner"]


def client_for(user: Any) -> Client:
    return signed_in(user, HTTP_ACCEPT_LANGUAGE="en")


def get(client: Client, url: str) -> str:
    response = client.get(url)
    assert response.status_code == 200, (url, response.status_code)
    return str(response.content.decode())


def project_page(client: Client, project: Project) -> str:
    return get(client, reverse("projects:detail", args=[project.pk]))


class TestEveryRoleReceivesAtAProject:
    @pytest.mark.parametrize("actor", ROLES)
    def test_discover_post_and_read_back(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy, actor: str
    ) -> None:
        user = getattr(people, actor)
        client = client_for(user)
        order = buy((make_line("10"), "10"))
        site = org.worksite(org.hospital)
        expected_url = reverse("receiving:expected", args=[org.hospital.pk])

        # Discover: the project page offers the action, the menu's project list leads to it.
        html = project_page(client, org.hospital)
        assert "Receive materials" in html and expected_url in html
        assert expected_url in page(client, "projects")

        # Read the expected deliveries and follow the way in to the form.
        html = page(client, "expected", org.hospital.pk)
        assert str(order.number) in html and "Receive these goods" in html
        receive_url = f"{reverse('receiving:receive', args=[order.pk])}?location={site.pk}"
        assert receive_url in html

        # Post it; a double tap carries the same key and makes one receipt.
        data = receipt_payload(client, order, site, delivery_note_ref="DN-SITE")
        url = reverse("receiving:post", args=[order.pk])
        first = client.post(url, data)
        second = client.post(url, data)
        receipt = GoodsReceipt.objects.get()
        detail_url = reverse("receiving:detail", args=[receipt.pk])
        assert first.status_code == second.status_code == 302
        assert first["Location"] == second["Location"] == detail_url
        assert (receipt.received_by, receipt.location) == (user, site)

        # Read it back: the page, the list and the list narrowed to the worksite.
        response = client.get(detail_url)
        assert response.status_code == 200
        html = response.content.decode()
        assert 'data-testid="receipt-number"' in html and "DN-SITE" in html
        assert expected_url in html  # the way back to the project's deliveries
        assert receipt.number in page(client, "list")
        assert receipt.number in page(client, "list", place=site.pk)

    @pytest.mark.parametrize("actor", ROLES)
    def test_a_closed_project_offers_no_action(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy, actor: str
    ) -> None:
        buy((make_line("10"), "10"))
        Project.objects.filter(pk=org.hospital.pk).update(status=Project.Status.CLOSED)
        # A site manager or a partner still reads a closed project they are assigned to.
        response = client_for(getattr(people, actor)).get(
            reverse("projects:detail", args=[org.hospital.pk])
        )
        if response.status_code == 200:
            assert "Receive materials" not in response.content.decode()

    @pytest.mark.parametrize("actor", ["mm_mad", "wo_mad", "site_manager"])
    @pytest.mark.parametrize("closed_by", ["project closed", "branch inactive"])
    def test_the_expected_deliveries_offer_no_form_where_nothing_can_arrive(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy, actor: str, closed_by: str
    ) -> None:
        """M9.3: the goods of a closed project arrive at a warehouse, and an inactive branch's
        worksite takes none (S9); the page still lists what is coming, without the way to
        a form that would refuse the place."""
        order = buy((make_line("10"), "10"))
        client = client_for(getattr(people, actor))
        assert "Receive these goods" in page(client, "expected", org.hospital.pk)
        if closed_by == "project closed":
            Project.objects.filter(pk=org.hospital.pk).update(status=Project.Status.CLOSED)
        else:  # B-MAD runs the hospital; A-MAD still reaches it as its buyer
            Branch.objects.filter(pk=org.b_mad.pk).update(is_active=False)
        response = client.get(reverse("receiving:expected", args=[org.hospital.pk]))
        if (actor, closed_by) == ("site_manager", "branch inactive"):
            # An assignment needs its project's branch active: no grant left at all (S7: 403).
            assert response.status_code == 403
            return
        html = response.content.decode()
        assert str(order.number) in html
        assert "Receive these goods" not in html

    def test_a_project_outside_the_scope_is_not_there(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy
    ) -> None:
        buy((make_line("10", project=org.sev_for_mad), "10"))
        partner = client_for(people.partner)  # assigned to the hospital only
        for name in ("expected",):
            response = partner.get(reverse(f"receiving:{name}", args=[org.sev_for_mad.pk]))
            assert response.status_code == 404
        assert org.sev_for_mad.code not in page(partner, "projects")


class TestTheEntryPoints:
    @pytest.mark.parametrize("actor", SITE_ONLY)
    def test_a_site_receiver_is_led_to_projects_not_to_orders(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy, actor: str
    ) -> None:
        order = buy((make_line("4"), "4"))
        client = client_for(getattr(people, actor))
        listing = page(client, "list")
        assert reverse("receiving:projects") in listing and "Receive materials" in listing
        assert reverse("receiving:orders") not in listing
        html = page(client, "orders")
        assert "Receive from a project" in html
        assert 'data-testid="receivable-order"' not in html and str(order.number) not in html
        assert 'name="q"' not in html  # no search of orders they cannot reach

    @pytest.mark.parametrize("actor", ["tech_mad", "partner"])
    def test_the_form_leads_back_to_the_project(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy, actor: str
    ) -> None:
        order = buy((make_line("4"), "4"))
        html = page(client_for(getattr(people, actor)), "receive", order.pk)
        assert reverse("receiving:expected", args=[org.hospital.pk]) in html
        assert f'href="{reverse("receiving:orders")}"' not in html

    @pytest.mark.parametrize("actor", ["wo_mad", "mm_mad", "engineer"])
    def test_the_branch_roles_keep_the_order_search(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy, actor: str
    ) -> None:
        order = buy((make_line("4"), "4"))
        client = client_for(getattr(people, actor))
        listing = page(client, "list")
        assert reverse("receiving:orders") in listing and "Receive goods" in listing
        assert reverse("receiving:orders") in page(client, "receive", order.pk)

    def test_a_mixed_user_keeps_the_order_search_of_their_branch_role(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy
    ) -> None:
        buy((make_line("4"), "4"))
        assert reverse("receiving:orders") in page(client_for(people.mixed), "list")

    def test_empty_states(self, org: Org, people: People) -> None:
        html = page(client_for(people.partner), "expected", org.hospital.pk)
        assert "Goods bought for this project will be listed here" in html
        Project.objects.filter(pk=org.hospital.pk).update(status=Project.Status.CLOSED)
        assert "No projects" in page(client_for(people.partner), "projects")


class TestWhatAProjectReceiverDoesNotSee:
    @pytest.mark.parametrize("actor", ["tech_mad", "partner"])
    def test_the_whole_flow_shows_only_the_projects_part(
        self, org: Org, people: People, actor: str
    ) -> None:
        sentinel = build_sentinel_order(org, people)
        order = sentinel.order
        client = client_for(getattr(people, actor))
        site = org.worksite(org.hospital)
        pages = [
            project_page(client, org.hospital),
            page(client, "projects"),
            page(client, "expected", org.hospital.pk),
            page(client, "receive", order.pk, location=site.pk),
            page(client, "list"),
        ]
        data = receipt_payload(client, order, site)
        response = client.post(reverse("receiving:post", args=[order.pk]), data, follow=True)
        receipt = GoodsReceipt.objects.get()
        pages.append(response.content.decode())
        pages.append(page(client, "list", place=site.pk))
        pages.append(page(client, "list", q=receipt.number))
        for html in pages:
            for leak in (
                Sentinel.PRICE,
                Sentinel.OTHER_PROJECT_QUANTITY,
                Sentinel.SUPPLIER_EMAIL,
                Sentinel.SUPPLIER_PHONE,
            ):
                assert not leak.found_in(html), leak
            if actor == "partner":  # a technician reaches SEV-2 too: A-MAD buys for it
                assert org.sev_for_mad.code not in html
            assert reverse("purchasing:detail", args=[order.pk]) not in html
            assert "/handovers/" not in html.replace(reverse("handovers:list"), "")
            assert 'data-field="remaining"' not in html
        # Only the supplier's display name, the order number and the project's share.
        detail = pages[5]
        assert Sentinel.SUPPLIER_NAME.value in detail and str(order.number) in detail
        assert re.search(r'data-field="accepted">\s*10 M', detail)

    def test_a_search_does_not_reach_what_is_not_theirs(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy, receive: Receive
    ) -> None:
        mine = receive(buy((make_line("5"), "5")), "5", at=org.worksite(org.hospital))
        other = receive(
            buy((make_line("5", project=org.sev_for_mad), "5")),
            "5",
            at=org.worksite(org.sev_for_mad),
        )
        client = client_for(people.partner)
        assert mine.number in page(client, "list", q=mine.number)
        assert "No receipts" in page(client, "list", q=other.number)
        assert client.get(reverse("receiving:detail", args=[other.pk])).status_code == 404

    @pytest.mark.parametrize("actor", ["tech_mad", "partner"])
    def test_the_receipt_has_its_attachments_panel_and_no_handover_link(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy, receive: Receive, actor: str
    ) -> None:
        receipt = receive(buy((make_line("5"), "5")), "5", at=org.worksite(org.hospital))
        html = get(
            client_for(getattr(people, actor)), reverse("receiving:detail", args=[receipt.pk])
        )
        assert 'data-testid="attachment-upload"' in html  # receipt.post adds a file (X8)
        assert "Handed over on site" not in html

    def test_a_site_manager_without_the_order_page(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy, receive: Receive
    ) -> None:
        order = buy((make_line("5"), "5"))
        receipt = receive(order, "5", at=org.worksite(org.hospital))
        html = get(client_for(people.site_manager), reverse("receiving:detail", args=[receipt.pk]))
        assert reverse("purchasing:detail", args=[order.pk]) not in html


class TestWithoutAWorksite:
    def test_a_site_receiver_with_nowhere_to_receive_is_sent_to_the_project_list(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy
    ) -> None:
        order = buy((make_line("4"), "4"))
        Project.objects.filter(pk=org.hospital.pk).update(status=Project.Status.CLOSED)
        html = page(client_for(people.site_manager), "receive", order.pk)
        assert "Nowhere to receive" in html
        assert f'href="{reverse("receiving:projects")}"' in html

    def test_a_warehouse_receipt_has_no_project_to_go_back_to(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy, receive: Receive
    ) -> None:
        receipt = receive(buy((make_line("4"), "4")), "4")
        html = get(client_for(people.wo_mad), reverse("receiving:detail", args=[receipt.pk]))
        assert reverse("receiving:expected", args=[org.hospital.pk]) not in html
