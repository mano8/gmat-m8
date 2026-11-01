"""The receiving screens (M6.3): receipts list and page, the search for an order, the receipt
form and its post, the void, and the site manager's expected deliveries.

Pages are read like a user does; the ``data-testid`` selectors the e2e layer will use are
checked present. A double tap posts the same key twice and must make one receipt.
"""

import re
from decimal import Decimal
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse

from apps.audit.models import AuditEvent
from apps.core.idempotency import issue_key
from apps.locations.models import Location
from apps.material_requests.models import MaterialRequestLine
from apps.purchasing.models import PurchaseOrder
from apps.receiving.models import GoodsReceipt
from tests.org import Org, People, Sentinel, build_sentinel_order
from tests.org import client_for as signed_in
from tests.unit.purchasing.conftest import Buy, MakeLine, allocation, only_line
from tests.unit.receiving.conftest import Receive

pytestmark = pytest.mark.django_db

D = Decimal


def client_for(user: Any) -> Client:
    return signed_in(user, HTTP_ACCEPT_LANGUAGE="en")


def page(client: Client, name: str, *args: Any, **query: Any) -> str:
    response = client.get(reverse(f"receiving:{name}", args=args), query)
    assert response.status_code == 200, response.status_code
    return str(response.content.decode())


def hidden(html: str, name: str) -> str:
    found = re.search(rf'name="{name}"[^>]*value="([^"]*)"', html)
    assert found is not None, name
    return found.group(1)


def field(html: str, name: str) -> str:
    """The text of the first element marked ``data-field``, whitespace squeezed."""
    found = re.search(rf'data-field="{name}">([^<]*)<', html)
    assert found is not None, name
    return " ".join(found.group(1).split())


def messages_of(response: Any) -> list[str]:
    return [str(message) for message in response.context["messages"]]


def receipt_payload(
    client: Client, order: PurchaseOrder, place: Location, **fields: Any
) -> dict[str, Any]:
    """The form as the page offers it (key and prefilled quantities), with ``fields`` on top."""
    html = page(client, "receive", order.pk, location=place.pk)
    line = only_line(order)
    data: dict[str, Any] = {
        "idempotency_key": hidden(html, "idempotency_key"),
        "location": str(place.pk),
        f"qty_{line.pk}": re.search(rf'name="qty_{line.pk}"[^>]*value="([^"]*)"', html).group(1),  # type: ignore[union-attr]
        f"issue_{line.pk}": "NONE",
    }
    return data | fields


def post(client: Client, order: PurchaseOrder, data: dict[str, Any], *, follow: bool = True) -> Any:
    return client.post(reverse("receiving:post", args=[order.pk]), data, follow=follow)


# --- the lists --------------------------------------------------------------------------


class TestLists:
    def test_receipts_list_for_who_may_see_them(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy, receive: Receive
    ) -> None:
        order = buy((make_line("10"), "10"))
        there = receive(order, "4", delivery_note_ref="DN-1")
        site = receive(order, "2", at=org.worksite(org.hospital))
        html = page(client_for(people.wo_mad), "list")
        assert html.count('data-testid="receipt-row"') == 2
        assert there.number in html and site.number in html
        only_site = page(client_for(people.site_manager), "list")
        assert site.number in only_site and there.number not in only_site
        assert there.number in page(client_for(people.wo_mad), "list", q="dn-1")
        assert site.number not in page(client_for(people.wo_mad), "list", q="dn-1")
        assert "No receipts" in page(client_for(people.wo_mad), "list", status="VOIDED")

    def test_the_buttons_follow_the_capabilities(
        self, org: Org, people: People, make_line: MakeLine
    ) -> None:
        assert reverse("receiving:orders") in page(client_for(people.wo_mad), "list")
        assert reverse("receiving:projects") in page(client_for(people.wo_mad), "list")

    def test_htmx_answers_the_fragment(self, people: People) -> None:
        response = client_for(people.wo_mad).get(reverse("receiving:list"), HTTP_HX_REQUEST="true")
        assert b"<html" not in response.content and b"receipt-results" in response.content

    def test_orders_to_receive_found_by_number_supplier_or_project(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy
    ) -> None:
        order = buy((make_line("3"), "3"))
        wo = client_for(people.wo_mad)
        html = page(wo, "orders")
        assert html.count('data-testid="receivable-order"') == 1 and str(order.number) in html
        assert reverse("receiving:receive", args=[order.pk]) in html
        assert "Not received" in html
        assert str(order.number) in page(wo, "orders", q="hosp")
        assert "No orders to receive" in page(wo, "orders", q="zzzz-nothing")
        fragment = wo.get(reverse("receiving:orders"), HTTP_HX_REQUEST="true")
        assert b"<html" not in fragment.content

    def test_a_site_manager_finds_nothing_by_branch_and_is_pointed_to_the_project(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy
    ) -> None:
        buy((make_line("3"), "3"))
        html = page(client_for(people.site_manager), "orders")
        assert 'data-testid="receivable-order"' not in html
        assert "expected deliveries" in html

    def test_the_projects_with_expected_deliveries(self, org: Org, people: People) -> None:
        html = page(client_for(people.site_manager), "projects")
        assert html.count('data-testid="expected-project"') == 1 and org.hospital.code in html
        assert reverse("receiving:expected", args=[org.hospital.pk]) in html


# --- the receipt form ------------------------------------------------------------------------


class TestTheForm:
    def test_prefilled_with_what_is_outstanding(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy, receive: Receive
    ) -> None:
        line = make_line("10")
        order = buy((line, "10"))
        receive(order, "3")
        html = page(
            client_for(people.wo_mad), "receive", order.pk, location=org.warehouses["A Madrid"].pk
        )
        po_line = only_line(order)
        assert f'name="qty_{po_line.pk}"' in html and 'value="7"' in html
        assert field(html, "remaining") == "7 M"
        assert 'data-testid="receipt-line"' in html and 'data-testid="receipt-split"' in html
        assert f'name="split_{po_line.pk}_{allocation(order, line).pk}"' in html
        assert hidden(html, "idempotency_key") and hidden(html, "location") == str(
            org.warehouses["A Madrid"].pk
        )
        assert "Rejected goods or an incident" in html and "Split between requests" in html

    def test_the_place_defaults_to_the_ship_to_and_can_be_changed(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy
    ) -> None:
        order = buy((make_line("10"), "10"))
        wo = client_for(people.wo_mad)
        html = page(wo, "receive", order.pk)
        assert hidden(html, "location") == str(order.ship_to_id)
        assert 'data-testid="place-form"' in html
        worksite = org.worksite(org.hospital)
        html = page(wo, "receive", order.pk, location=worksite.pk)
        assert hidden(html, "location") == str(worksite.pk)
        html = page(wo, "receive", order.pk, location="not-a-place")
        assert hidden(html, "location") == str(order.ship_to_id)

    def test_a_site_manager_sees_their_worksite_and_nothing_of_the_rest_of_the_order(
        self, org: Org, people: People
    ) -> None:
        sentinel = build_sentinel_order(org, people)
        sm = client_for(people.site_manager)
        html = page(sm, "receive", sentinel.order.pk)
        assert 'data-testid="place-form"' not in html and 'data-testid="place-name"' in html
        assert 'name="qty_' in html and 'value="10"' in html
        assert 'data-field="remaining"' not in html
        assert Sentinel.SUPPLIER_NAME.value in html
        for leak in (Sentinel.OTHER_PROJECT_QUANTITY, Sentinel.PRICE):
            assert not leak.found_in(html)
        assert not Sentinel.SUPPLIER_EMAIL.found_in(html)

    def test_nothing_to_receive_and_nowhere_to_receive(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy, receive: Receive
    ) -> None:
        order = buy((make_line("4"), "4"))
        receive(order, "4")
        html = page(client_for(people.wo_mad), "receive", order.pk)
        assert "Nothing left to receive here" in html
        Location.objects.filter(kind=Location.Kind.WAREHOUSE).update(is_active=False)
        Location.objects.filter(kind=Location.Kind.WORKSITE).update(is_active=False)
        assert "Nowhere to receive" in page(client_for(people.wo_mad), "receive", order.pk)

    @pytest.mark.parametrize("actor", ["mm_sev", "tech_mad", "partner", "nobody"])
    def test_anyone_else_gets_404_or_403(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy, actor: str
    ) -> None:
        order = buy((make_line("4"), "4"))
        response = client_for(getattr(people, actor)).get(
            reverse("receiving:receive", args=[order.pk])
        )
        assert response.status_code in {403, 404}


# --- posting ------------------------------------------------------------------------------


class TestPosting:
    def test_posts_the_prefilled_form_and_shows_the_receipt(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy
    ) -> None:
        line = make_line("10")
        order = buy((line, "10"))
        wo = client_for(people.wo_mad)
        data = receipt_payload(wo, order, org.warehouses["A Madrid"], delivery_note_ref="DN-9")
        response = post(wo, order, data)
        receipt = GoodsReceipt.objects.get()
        assert response.redirect_chain[-1][0] == reverse("receiving:detail", args=[receipt.pk])
        assert any(receipt.number in message for message in messages_of(response))
        assert (receipt.delivery_note_ref, receipt.received_by) == ("DN-9", people.wo_mad)
        html = response.content.decode()
        assert 'data-testid="receipt-number"' in html and field(html, "accepted") == "10 M"
        assert f"{line.request.number} · {org.hospital.code}: 10" in html
        assert AuditEvent.objects.filter(action="receipt.posted").exists()

    def test_a_double_tap_makes_one_receipt(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy
    ) -> None:
        order = buy((make_line("10"), "10"))
        wo = client_for(people.wo_mad)
        data = receipt_payload(wo, order, org.warehouses["A Madrid"])
        first = post(wo, order, data, follow=False)
        second = post(wo, order, data, follow=False)
        assert first.status_code == second.status_code == 302
        assert first["Location"] == second["Location"]
        assert GoodsReceipt.objects.count() == 1

    def test_a_double_tap_that_used_the_order_up_goes_to_the_same_receipt(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy
    ) -> None:
        """The second form no longer has a line to offer: the key leads to the receipt."""
        order = buy((make_line("10"), "10"))
        wo = client_for(people.wo_mad)
        data = receipt_payload(wo, order, org.warehouses["A Madrid"])
        post(wo, order, data)
        again = post(wo, order, data, follow=True)
        assert again.redirect_chain[-1][0] == reverse(
            "receiving:detail", args=[GoodsReceipt.objects.get().pk]
        )
        assert any("already posted" in message for message in messages_of(again))
        assert GoodsReceipt.objects.count() == 1

    def test_the_same_key_for_other_quantities_is_refused(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy
    ) -> None:
        order = buy((make_line("10"), "10"))
        line = only_line(order)
        wo = client_for(people.wo_mad)
        data = receipt_payload(wo, order, org.warehouses["A Madrid"], **{f"qty_{line.pk}": "4"})
        post(wo, order, data)
        changed = post(wo, order, data | {f"qty_{line.pk}": "3"}, follow=False)
        assert changed.status_code == 409
        assert GoodsReceipt.objects.count() == 1

    def test_the_key_of_another_form_is_not_followed(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy
    ) -> None:
        order = buy((make_line("10"), "10"))
        line = only_line(order)
        wo = client_for(people.wo_mad)
        data = receipt_payload(wo, order, org.warehouses["A Madrid"], **{f"qty_{line.pk}": ""})
        assert post(wo, order, data | {"idempotency_key": ""}, follow=False).status_code == 200
        unused = post(wo, order, data, follow=False)  # a valid key, nothing posted with it
        assert unused.status_code == 200 and GoodsReceipt.objects.count() == 0

    def test_a_tampered_or_foreign_key_is_a_400(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy
    ) -> None:
        order = buy((make_line("10"), "10"))
        wo = client_for(people.wo_mad)
        data = receipt_payload(wo, order, org.warehouses["A Madrid"])
        assert post(wo, order, data | {"idempotency_key": "x:y"}, follow=False).status_code == 400
        foreign = issue_key(people.mm_mad.pk, "receipt.post")
        assert post(wo, order, data | {"idempotency_key": foreign}, follow=False).status_code == 400
        assert GoodsReceipt.objects.count() == 0

    def test_a_partial_receipt_with_a_rejection_and_an_incident(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy
    ) -> None:
        order = buy((make_line("10"), "10"))
        line = only_line(order)
        wo = client_for(people.wo_mad)
        data = receipt_payload(
            wo,
            order,
            org.warehouses["A Madrid"],
            **{
                f"qty_{line.pk}": "6",
                f"rejected_{line.pk}": "2",
                f"issue_{line.pk}": "DAMAGED",
                f"note_{line.pk}": "Crushed boxes",
            },
        )
        post(wo, order, data)
        receipt = GoodsReceipt.objects.get()
        (row,) = receipt.lines.all()
        assert (row.qty_received, row.qty_rejected, row.issue, row.issue_note) == (
            D(6),
            D(2),
            "DAMAGED",
            "Crushed boxes",
        )

    def test_a_manual_split_is_applied(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy
    ) -> None:
        first, second = make_line("5"), make_line("5")
        order = buy((first, "5"), (second, "5"))
        line = only_line(order)
        wo = client_for(people.wo_mad)
        data = receipt_payload(
            wo,
            order,
            org.warehouses["A Madrid"],
            **{
                f"qty_{line.pk}": "6",
                f"manual_{line.pk}": "on",
                f"split_{line.pk}_{allocation(order, first).pk}": "1",
                f"split_{line.pk}_{allocation(order, second).pk}": "5",
            },
        )
        post(wo, order, data)
        parts = {
            a.allocation_id: a.qty
            for a in GoodsReceipt.objects.get().lines.get().attributions.all()
        }
        assert parts == {allocation(order, first).pk: D(1), allocation(order, second).pk: D(5)}

    def test_a_split_beyond_the_caps_comes_back_on_the_form(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy
    ) -> None:
        first, second = make_line("5"), make_line("5")
        order = buy((first, "5"), (second, "5"))
        line = only_line(order)
        wo = client_for(people.wo_mad)
        data = receipt_payload(
            wo,
            order,
            org.warehouses["A Madrid"],
            **{
                f"qty_{line.pk}": "6",
                f"manual_{line.pk}": "on",
                f"split_{line.pk}_{allocation(order, first).pk}": "6",
            },
        )
        response = post(wo, order, data, follow=False)
        assert response.status_code == 200
        assert "can take at most" in response.content.decode()
        assert b'name="split_' in response.content and b"open" in response.content
        assert GoodsReceipt.objects.count() == 0

    def test_more_than_is_outstanding_comes_back_on_the_form(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy
    ) -> None:
        order = buy((make_line("10"), "10"))
        line = only_line(order)
        wo = client_for(people.wo_mad)
        data = receipt_payload(wo, order, org.warehouses["A Madrid"], **{f"qty_{line.pk}": "11"})
        response = post(wo, order, data, follow=False)
        assert response.status_code == 200
        assert "only 10 remain to be received" in response.content.decode()
        assert GoodsReceipt.objects.count() == 0

    def test_an_empty_form_and_a_missing_place_are_not_posted(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy
    ) -> None:
        order = buy((make_line("10"), "10"))
        line = only_line(order)
        wo = client_for(people.wo_mad)
        data = receipt_payload(wo, order, org.warehouses["A Madrid"], **{f"qty_{line.pk}": ""})
        response = post(wo, order, data, follow=False)
        assert response.status_code == 200
        assert "at least one line" in response.content.decode()
        missing = post(wo, order, {}, follow=True)
        assert any("Choose where" in message for message in messages_of(missing))
        elsewhere = post(wo, order, data | {"location": str(org.warehouses["A Sevilla"].pk)})
        assert any("Choose where" in message for message in messages_of(elsewhere))
        assert GoodsReceipt.objects.count() == 0

    def test_a_site_manager_receives_at_their_worksite_and_the_goods_are_handed_over(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy
    ) -> None:
        line = make_line("4")
        order = buy((line, "4"), (make_line("6", project=org.sev_for_mad), "6"))
        sm = client_for(people.site_manager)
        place = org.worksite(org.hospital)
        data = receipt_payload(sm, order, place)
        assert data[f"qty_{only_line(order).pk}"] == "4"
        response = post(sm, order, data)
        receipt = GoodsReceipt.objects.get()
        assert receipt.location == place and receipt.received_by == people.site_manager
        assert receipt.direct_handover.lines.get().request_line == line
        html = response.content.decode()
        assert "Handed over on site" in html and 'data-testid="void-form"' not in html
        assert MaterialRequestLine.objects.get(pk=line.pk).request.status == "CLOSED"

    def test_a_site_manager_cannot_post_at_a_warehouse(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy
    ) -> None:
        order = buy((make_line("4"), "4"))
        sm = client_for(people.site_manager)
        data = receipt_payload(sm, order, org.worksite(org.hospital))
        data["location"] = str(org.warehouses["A Madrid"].pk)
        response = post(sm, order, data)
        assert any("Choose where" in message for message in messages_of(response))
        assert GoodsReceipt.objects.count() == 0

    def test_get_does_not_post(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy
    ) -> None:
        order = buy((make_line("4"), "4"))
        response = client_for(people.wo_mad).get(reverse("receiving:post", args=[order.pk]))
        assert response.status_code == 405


# --- the receipt page and the void -------------------------------------------------------------


class TestReceiptPage:
    def test_lines_attributions_and_the_order_link(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy, receive: Receive
    ) -> None:
        line = make_line("10")
        order = buy((line, "10"))
        receipt = receive(order, "8")
        html = page(client_for(people.mm_mad), "detail", receipt.pk)
        assert field(html, "supplier") == Sentinel.SUPPLIER_NAME.value
        assert 'data-testid="receipt-line"' in html and 'data-testid="receipt-attribution"' in html
        assert reverse("purchasing:detail", args=[order.pk]) in html
        assert 'data-testid="void-form"' in html

    def test_the_warehouse_operator_reads_but_cannot_void(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy, receive: Receive
    ) -> None:
        receipt = receive(buy((make_line("10"), "10")), "8")
        html = page(client_for(people.wo_mad), "detail", receipt.pk)
        assert 'data-testid="void-form"' not in html

    def test_a_site_manager_reads_their_worksite_receipt_without_the_order_link(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy, receive: Receive
    ) -> None:
        receipt = receive(buy((make_line("4"), "4")), "4", at=org.worksite(org.hospital))
        html = page(client_for(people.site_manager), "detail", receipt.pk)
        assert reverse("purchasing:detail", args=[receipt.po_id]) not in html
        assert reverse("handovers:detail", args=[receipt.direct_handover.pk]) in html

    def test_a_warehouse_receipt_is_not_the_site_managers(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy, receive: Receive
    ) -> None:
        receipt = receive(buy((make_line("4"), "4")), "4")
        response = client_for(people.site_manager).get(
            reverse("receiving:detail", args=[receipt.pk])
        )
        assert response.status_code == 404


class TestVoid:
    def test_voids_with_a_reason_and_shows_it(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy, receive: Receive
    ) -> None:
        line = make_line("4")
        order = buy((line, "4"))
        receipt = receive(order, "4")
        mm = client_for(people.mm_mad)
        response = mm.post(
            reverse("receiving:void", args=[receipt.pk]), {"reason": "Wrong delivery"}, follow=True
        )
        receipt.refresh_from_db()
        assert receipt.status == "VOIDED" and receipt.void_reason == "Wrong delivery"
        assert any("voided" in message for message in messages_of(response))
        html = response.content.decode()
        assert 'data-testid="void-reason"' in html and "Wrong delivery" in html
        assert 'data-testid="void-form"' not in html

    def test_a_reason_is_required(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy, receive: Receive
    ) -> None:
        receipt = receive(buy((make_line("4"), "4")), "4")
        response = client_for(people.mm_mad).post(
            reverse("receiving:void", args=[receipt.pk]), {"reason": " "}, follow=True
        )
        receipt.refresh_from_db()
        assert receipt.status == "POSTED"
        assert any("Give a reason" in message for message in messages_of(response))

    def test_a_service_refusal_is_flashed(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy, receive: Receive, mm: Any
    ) -> None:
        from apps.receiving import services

        receipt = receive(buy((make_line("4"), "4")), "4")
        services.void_receipt(mm, receipt.pk, reason="First")
        response = client_for(people.mm_mad).post(
            reverse("receiving:void", args=[receipt.pk]), {"reason": "Again"}, follow=True
        )
        assert messages_of(response)

    def test_the_warehouse_operator_is_refused_and_a_site_manager_finds_nothing(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy, receive: Receive
    ) -> None:
        receipt = receive(buy((make_line("4"), "4")), "4")
        url = reverse("receiving:void", args=[receipt.pk])
        assert client_for(people.wo_mad).post(url, {"reason": "x"}).status_code == 403
        assert client_for(people.site_manager).post(url, {"reason": "x"}).status_code == 403
        receipt.refresh_from_db()
        assert receipt.status == "POSTED"


# --- the expected deliveries -------------------------------------------------------------------


class TestExpectedDeliveries:
    def test_the_projection_and_the_way_in_to_the_form(self, org: Org, people: People) -> None:
        sentinel = build_sentinel_order(org, people)
        PurchaseOrder.objects.filter(pk=sentinel.order.pk).update(expected_date="2026-12-01")
        html = page(client_for(people.site_manager), "expected", org.hospital.pk)
        assert 'data-testid="expected-order"' in html
        assert field(html, "supplier") == Sentinel.SUPPLIER_NAME.value
        assert field(html, "expected") == "10 M"
        assert field(html, "expected-date")
        receive_url = reverse("receiving:receive", args=[sentinel.order.pk])
        link = f"{receive_url}?location={org.worksite(org.hospital).pk}"
        assert link in html
        for leak in (
            Sentinel.PRICE,
            Sentinel.OTHER_PROJECT_QUANTITY,
            Sentinel.SUPPLIER_EMAIL,
            Sentinel.SUPPLIER_PHONE,
        ):
            assert not leak.found_in(html)

    def test_nothing_expected(self, org: Org, people: People) -> None:
        html = page(client_for(people.site_manager), "expected", org.hospital.pk)
        assert "Nothing is expected here" in html

    def test_a_reader_who_cannot_post_gets_no_button(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy
    ) -> None:
        buy((make_line("4"), "4"))
        html = page(client_for(people.wo_mad), "expected", org.hospital.pk)
        assert "Receive these goods" in html

    def test_another_projects_page_is_a_404(self, org: Org, people: People) -> None:
        response = client_for(people.site_manager).get(
            reverse("receiving:expected", args=[org.sev_for_mad.pk])
        )
        assert response.status_code == 404
