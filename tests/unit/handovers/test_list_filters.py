"""The filters M7d.2 added to the order, receipt and handover lists, and the expected-deliveries
search: each only narrows what the scope already shows."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext

from apps.access.context import AccessContext
from apps.accounts.models import User
from apps.handovers import selectors as handover_selectors
from apps.handovers.models import Handover
from apps.purchasing import selectors as purchasing_selectors
from apps.receiving import selectors as receiving_selectors
from apps.receiving.models import GoodsReceipt
from tests.org import Org, People, client_for
from tests.unit.handovers.conftest import HandOver
from tests.unit.purchasing.conftest import Buy, MakeLine
from tests.unit.receiving.conftest import Receive

pytestmark = pytest.mark.django_db

D = Decimal


def ctx(user: User) -> AccessContext:
    return AccessContext.for_user(user)


class TestOrders:
    def test_project_and_text(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy
    ) -> None:
        order = buy((make_line("5"), "5"))
        mm = ctx(people.mm_mad)
        assert list(purchasing_selectors.orders(mm, project_id=org.hospital.pk)) == [order]
        assert not purchasing_selectors.orders(mm, project_id=org.sev_for_mad.pk).exists()
        assert list(purchasing_selectors.orders(mm, q=str(order.number).lower())) == [order]
        assert list(purchasing_selectors.orders(mm, q=org.supplier.name[:5])) == [order]
        assert not purchasing_selectors.orders(mm, q="no such order").exists()

    def test_the_project_choices_are_the_projects_the_user_sees(
        self, org: Org, people: People
    ) -> None:
        choices = purchasing_selectors.order_projects(ctx(people.mm_mad))
        assert org.hospital in choices
        assert org.sev_works not in choices

    def test_a_project_outside_the_scope_is_not_a_choice(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy
    ) -> None:
        buy((make_line("5"), "5"))
        response = client_for(people.mm_sev).get(
            reverse("purchasing:orders"), {"project": str(org.hospital.pk), "status": ""}
        )
        assert response.status_code == 200
        assert 'data-testid="order-row"' not in response.content.decode()


class TestReceipts:
    def test_place_and_dates(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy, receive: Receive
    ) -> None:
        receipt = receive(buy((make_line("5"), "5")), "5")
        mm = ctx(people.mm_mad)
        today = timezone.localdate()
        place = org.warehouses["A Madrid"].pk
        assert list(receiving_selectors.receipts(mm, location_id=place)) == [receipt]
        assert not receiving_selectors.receipts(mm, location_id=org.worksite(org.hospital).pk)
        assert list(receiving_selectors.receipts(mm, received_from=today)) == [receipt]
        assert list(receiving_selectors.receipts(mm, received_to=today)) == [receipt]
        tomorrow = today + timedelta(days=1)
        assert not receiving_selectors.receipts(mm, received_from=tomorrow)
        assert not receiving_selectors.receipts(mm, received_to=today - timedelta(days=1))

    def test_the_places_are_the_ones_the_user_reads(self, org: Org, people: People) -> None:
        places = receiving_selectors.receipt_places(ctx(people.mm_mad))
        assert org.warehouses["A Madrid"] in places
        assert org.warehouses["A Sevilla"] not in places

    def test_an_inverted_range_is_reported(self, org: Org, people: People) -> None:
        response = client_for(people.mm_mad).get(
            reverse("receiving:list"), {"received_from": "2026-02-01", "received_to": "2026-01-01"}
        )
        assert response.status_code == 200
        assert gettext("The end date cannot be before the start date.") in response.content.decode()

    def test_expected_projects_search(self, org: Org, people: People) -> None:
        site = ctx(people.site_manager)
        assert list(receiving_selectors.expected_projects(site, q=org.hospital.code.lower()))
        assert not receiving_selectors.expected_projects(site, q="zzzz-none")


class TestHandovers:
    def test_dates(
        self, org: Org, people: People, make_line: MakeLine, hand_over: HandOver
    ) -> None:
        stock: Handover = hand_over((make_line("10", sourcing="STOCK"), "4"))
        mm = ctx(people.mm_mad)
        today: date = timezone.localdate()
        assert list(handover_selectors.handovers(mm, handed_from=today, handed_to=today)) == [stock]
        assert not handover_selectors.handovers(mm, handed_from=today + timedelta(days=1))
        assert not handover_selectors.handovers(mm, handed_to=today - timedelta(days=1))

    def test_an_inverted_range_is_reported(self, org: Org, people: People) -> None:
        response = client_for(people.mm_mad).get(
            reverse("handovers:list"), {"handed_from": "2026-02-01", "handed_to": "2026-01-01"}
        )
        assert gettext("The end date cannot be before the start date.") in response.content.decode()


def test_receipt_status_stays_a_filter(
    org: Org, people: People, make_line: MakeLine, buy: Buy, receive: Receive
) -> None:
    receipt = receive(buy((make_line("5"), "5")), "5")
    mm = ctx(people.mm_mad)
    assert list(receiving_selectors.receipts(mm, status=GoodsReceipt.Status.POSTED)) == [receipt]
    assert not receiving_selectors.receipts(mm, status=GoodsReceipt.Status.VOIDED)
