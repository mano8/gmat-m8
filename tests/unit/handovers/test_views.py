"""The handover screens (M6.3): list and page, the form (from a project or a request), the
stock warning, the post (a double tap makes one handover) and the void.
"""

import re
from decimal import Decimal
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse

from apps.audit.models import AuditEvent
from apps.handovers.models import Handover
from apps.locations.models import Location
from apps.material_requests.models import MaterialRequestLine
from tests.org import Org, People
from tests.org import client_for as signed_in
from tests.unit.handovers.conftest import HandOver
from tests.unit.purchasing.conftest import Buy, MakeLine
from tests.unit.receiving.conftest import Receive

pytestmark = pytest.mark.django_db

D = Decimal


def client_for(user: Any) -> Client:
    return signed_in(user, HTTP_ACCEPT_LANGUAGE="en")


def page(client: Client, name: str, *args: Any, **query: Any) -> str:
    response = client.get(reverse(f"handovers:{name}", args=args), query)
    assert response.status_code == 200, response.status_code
    return str(response.content.decode())


def hidden(html: str, name: str) -> str:
    found = re.search(rf'name="{name}"[^>]*value="([^"]*)"', html)
    assert found is not None, name
    return found.group(1)


def field(html: str, name: str) -> str:
    found = re.search(rf'data-field="{name}">([^<]*)<', html)
    assert found is not None, name
    return " ".join(found.group(1).split())


def messages_of(response: Any) -> list[str]:
    return [str(message) for message in response.context["messages"]]


def form_data(
    client: Client, line: MaterialRequestLine, org: Org, people: People, **fields: Any
) -> dict[str, Any]:
    """The form as the page offers it, filled with 4 for ``line`` and the partner as receiver."""
    html = page(client, "new", project=line.project_id)
    data: dict[str, Any] = {
        "idempotency_key": hidden(html, "idempotency_key"),
        "project": str(line.project_id),
        "source": str(org.warehouses["A Madrid"].pk),
        "receiver_user": str(people.partner.pk),
        f"qty_{line.pk}": "4",
    }
    return data | fields


def create(client: Client, data: dict[str, Any], *, follow: bool = True) -> Any:
    return client.post(reverse("handovers:create"), data, follow=follow)


@pytest.fixture
def stocked(org: Org, make_line: MakeLine, buy: Buy, receive: Receive) -> MaterialRequestLine:
    """A request line of the hospital whose 10 arrived at A-MAD's warehouse (so no warning)."""
    line = make_line("10")
    receive(buy((line, "10")), "10")
    return line


# --- the lists ----------------------------------------------------------------------------


class TestLists:
    def test_scope_and_filters(
        self, org: Org, people: People, make_line: MakeLine, hand_over: HandOver
    ) -> None:
        line = make_line("10", sourcing="STOCK")
        stock = hand_over((line, "4"))
        html = page(client_for(people.mm_mad), "list")
        assert html.count('data-testid="handover-row"') == 1 and stock.number in html
        assert stock.number in page(client_for(people.site_manager), "list")
        assert stock.number in page(client_for(people.mm_mad), "list", q=org.hospital.code)
        assert "No handovers" in page(client_for(people.mm_mad), "list", status="VOIDED")
        assert "No handovers" in page(client_for(people.mm_mad), "list", kind="DIRECT_RECEIPT")
        by_project = page(client_for(people.mm_mad), "list", project=org.hospital.pk)
        assert stock.number in by_project
        fragment = client_for(people.mm_mad).get(reverse("handovers:list"), HTTP_HX_REQUEST="true")
        assert b"<html" not in fragment.content

    def test_a_handover_to_a_typed_name_is_listed(
        self, org: Org, people: People, stocked: MaterialRequestLine
    ) -> None:
        """The receiver is a user or a typed name; the list shows either (it answered 500 for
        a name, found on the volume dataset, M7d.4)."""
        wo = client_for(people.wo_mad)
        data = form_data(wo, stocked, org, people, receiver_user="", receiver_name="Pat Taker")
        create(wo, data)
        response = client_for(people.mm_mad).get(reverse("handovers:list"))
        assert response.status_code == 200
        assert "Pat Taker" in response.content.decode()

    def test_the_new_button_is_for_who_may_post(self, org: Org, people: People) -> None:
        new = reverse("handovers:new")
        assert new in page(client_for(people.wo_mad), "list")
        assert new not in page(client_for(people.site_manager), "list")

    def test_a_technician_has_no_handovers(self, people: People) -> None:
        assert client_for(people.tech_mad).get(reverse("handovers:list")).status_code == 403


# --- the form --------------------------------------------------------------------------------


class TestTheForm:
    def test_without_a_project_it_lists_the_projects(
        self, org: Org, people: People, make_line: MakeLine
    ) -> None:
        make_line("10", sourcing="STOCK")
        html = page(client_for(people.wo_mad), "new")
        assert html.count('data-testid="handover-project"') == 1
        assert f"?project={org.hospital.pk}" in html

    def test_nothing_to_hand_over(self, org: Org, people: People) -> None:
        assert "Nothing to hand over" in page(client_for(people.wo_mad), "new")

    def test_the_lines_of_a_project_with_what_is_outstanding(
        self, org: Org, people: People, make_line: MakeLine, hand_over: HandOver
    ) -> None:
        line = make_line("10", sourcing="STOCK")
        hand_over((line, "4"))
        html = page(client_for(people.wo_mad), "new", project=org.hospital.pk)
        assert html.count('data-testid="handover-line"') == 1
        assert field(html, "outstanding") == "6 M"
        assert f'name="qty_{line.pk}"' in html
        assert hidden(html, "idempotency_key") and hidden(html, "project") == str(org.hospital.pk)
        assert 'data-testid="stock-warning"' not in html

    def test_without_a_warehouse_it_says_why_instead_of_an_empty_choice(
        self, org: Org, people: People, make_line: MakeLine
    ) -> None:
        make_line("10", sourcing="STOCK")
        Location.objects.filter(kind=Location.Kind.WAREHOUSE).update(is_active=False)
        html = " ".join(page(client_for(people.wo_mad), "new", project=org.hospital.pk).split())
        assert 'data-testid="no-source"' in html and 'data-testid="handover-form"' not in html
        assert f"an active warehouse of {org.a_mad.name}" in html

    def test_from_a_request(self, org: Org, people: People, make_line: MakeLine) -> None:
        first, second = make_line("10", sourcing="STOCK"), make_line("5", sourcing="STOCK")
        html = page(client_for(people.wo_mad), "new", request=str(second.request_id))
        assert f'name="qty_{second.pk}"' in html and f'name="qty_{first.pk}"' not in html
        assert hidden(html, "request") == str(second.request_id)
        assert (second.request.number or "") in html

    def test_the_receivers_and_warehouses_are_the_selectors_choices(
        self, org: Org, people: People, make_line: MakeLine
    ) -> None:
        make_line("10", sourcing="STOCK")
        html = page(client_for(people.wo_mad), "new", project=org.hospital.pk)
        assert str(org.warehouses["A Madrid"].pk) in html
        assert str(org.warehouses["A Sevilla"].pk) not in html
        assert str(people.partner.pk) in html and str(people.site_manager.pk) in html
        assert str(people.nobody.pk) not in html

    @pytest.mark.parametrize("actor", ["site_manager", "tech_mad", "partner"])
    def test_without_the_capability_it_is_a_403(
        self, org: Org, people: People, make_line: MakeLine, actor: str
    ) -> None:
        line = make_line("10", sourcing="STOCK")
        client = client_for(getattr(people, actor))
        url = reverse("handovers:new")
        assert client.get(url, {"project": str(org.hospital.pk)}).status_code == 403
        assert client.get(url, {"request": str(line.request_id)}).status_code == 403

    def test_a_foreign_or_unknown_project_is_a_404(
        self, org: Org, people: People, make_line: MakeLine
    ) -> None:
        line = make_line("10", sourcing="STOCK")
        wo = client_for(people.wo_mad)
        # The hospital is bought for by A-MAD: A-SEV's material manager has no say in it.
        sev = client_for(people.mm_sev)
        hospital = {"project": str(org.hospital.pk)}
        assert sev.get(reverse("handovers:new"), hospital).status_code == 404
        assert (
            sev.get(reverse("handovers:new"), {"request": str(line.request_id)}).status_code == 404
        )
        missing = {"project": "00000000-0000-7000-8000-000000000000"}
        assert wo.get(reverse("handovers:new"), missing).status_code == 404
        assert wo.get(reverse("handovers:new"), {"request": missing["project"]}).status_code == 404

    def test_a_malformed_id_is_ignored(self, org: Org, people: People, make_line: MakeLine) -> None:
        make_line("10", sourcing="STOCK")
        html = page(client_for(people.wo_mad), "new", project="nonsense")
        assert 'data-testid="handover-projects"' in html


# --- posting -------------------------------------------------------------------------------------


class TestPosting:
    def test_hands_the_material_over_and_shows_the_handover(
        self, org: Org, people: People, stocked: MaterialRequestLine
    ) -> None:
        wo = client_for(people.wo_mad)
        response = create(wo, form_data(wo, stocked, org, people))
        handover = Handover.objects.get()
        assert response.redirect_chain[-1][0] == reverse("handovers:detail", args=[handover.pk])
        assert any(handover.number in message for message in messages_of(response))
        assert (handover.received_by, handover.handed_by) == (people.partner, people.wo_mad)
        html = response.content.decode()
        assert 'data-testid="handover-number"' in html and field(html, "qty") == "4 M"
        assert AuditEvent.objects.filter(action="handover.posted").exists()

    def test_a_typed_name_is_a_receiver(
        self, org: Org, people: People, stocked: MaterialRequestLine
    ) -> None:
        wo = client_for(people.wo_mad)
        data = form_data(wo, stocked, org, people, receiver_user="", receiver_name="Pat Taker")
        response = create(wo, data)
        assert Handover.objects.get().received_by_name == "Pat Taker"
        assert field(response.content.decode(), "received-by") == "Pat Taker"

    def test_a_person_and_a_name_together_come_back_on_the_form(
        self, org: Org, people: People, stocked: MaterialRequestLine
    ) -> None:
        wo = client_for(people.wo_mad)
        data = form_data(wo, stocked, org, people, receiver_name="Pat Taker")
        response = create(wo, data, follow=False)
        assert response.status_code == 200
        assert "not both" in response.content.decode()
        assert Handover.objects.count() == 0

    def test_a_double_tap_makes_one_handover(
        self, org: Org, people: People, stocked: MaterialRequestLine
    ) -> None:
        wo = client_for(people.wo_mad)
        data = form_data(wo, stocked, org, people)
        first = create(wo, data, follow=False)
        second = create(wo, data, follow=False)
        assert first.status_code == second.status_code == 302
        assert first["Location"] == second["Location"]
        assert Handover.objects.count() == 1

    def test_a_double_tap_that_handed_the_last_over_goes_to_the_same_handover(
        self, org: Org, people: People, stocked: MaterialRequestLine
    ) -> None:
        wo = client_for(people.wo_mad)
        data = form_data(wo, stocked, org, people, **{f"qty_{stocked.pk}": "10"})
        create(wo, data)
        again = create(wo, data)
        assert again.redirect_chain[-1][0] == reverse(
            "handovers:detail", args=[Handover.objects.get().pk]
        )
        assert any("already posted" in message for message in messages_of(again))
        assert Handover.objects.count() == 1

    def test_the_same_key_for_other_quantities_is_refused(
        self, org: Org, people: People, stocked: MaterialRequestLine
    ) -> None:
        wo = client_for(people.wo_mad)
        data = form_data(wo, stocked, org, people)
        create(wo, data)
        changed = create(wo, data | {f"qty_{stocked.pk}": "2"}, follow=False)
        assert changed.status_code == 409
        assert Handover.objects.count() == 1

    def test_a_tampered_key_is_a_400(
        self, org: Org, people: People, stocked: MaterialRequestLine
    ) -> None:
        wo = client_for(people.wo_mad)
        data = form_data(wo, stocked, org, people)
        assert create(wo, data | {"idempotency_key": "x:y"}, follow=False).status_code == 400
        invalid = create(
            wo, data | {"idempotency_key": "x:y", f"qty_{stocked.pk}": ""}, follow=False
        )
        assert invalid.status_code == 400

    def test_more_than_is_outstanding_comes_back_on_the_form(
        self, org: Org, people: People, stocked: MaterialRequestLine
    ) -> None:
        wo = client_for(people.wo_mad)
        data = form_data(wo, stocked, org, people, acknowledge="on", **{f"qty_{stocked.pk}": "11"})
        response = create(wo, data, follow=False)
        assert response.status_code == 200
        assert "only 10 remain to be handed over" in response.content.decode()
        assert Handover.objects.count() == 0

    def test_an_empty_form_is_not_posted(
        self, org: Org, people: People, stocked: MaterialRequestLine
    ) -> None:
        wo = client_for(people.wo_mad)
        data = form_data(wo, stocked, org, people, **{f"qty_{stocked.pk}": ""})
        response = create(wo, data, follow=False)
        assert response.status_code == 200
        assert "at least one line" in response.content.decode()

    def test_without_a_project_it_goes_back_to_the_choice(self, people: People) -> None:
        response = create(client_for(people.wo_mad), {})
        assert any("Choose a project" in message for message in messages_of(response))
        assert Handover.objects.count() == 0

    def test_a_warehouse_of_another_branch_is_not_a_choice(
        self, org: Org, people: People, stocked: MaterialRequestLine
    ) -> None:
        wo = client_for(people.wo_mad)
        data = form_data(wo, stocked, org, people, source=str(org.warehouses["A Sevilla"].pk))
        assert create(wo, data, follow=False).status_code == 200
        assert Handover.objects.count() == 0

    def test_get_does_not_post(self, people: People) -> None:
        assert client_for(people.wo_mad).get(reverse("handovers:create")).status_code == 405

    @pytest.mark.parametrize("actor", ["site_manager", "tech_mad", "partner"])
    def test_without_the_capability_it_is_a_403(
        self, org: Org, people: People, stocked: MaterialRequestLine, actor: str
    ) -> None:
        data = form_data(client_for(people.wo_mad), stocked, org, people)
        assert create(client_for(getattr(people, actor)), data, follow=False).status_code == 403


class TestTheStockWarning:
    def test_a_shortfall_comes_back_once_and_the_tick_posts(
        self, org: Org, people: People, make_line: MakeLine
    ) -> None:
        line = make_line("10", sourcing="STOCK")  # nothing ever received at the warehouse
        wo = client_for(people.wo_mad)
        data = form_data(wo, line, org, people)
        warned = create(wo, data, follow=False)
        html = warned.content.decode()
        assert warned.status_code == 200 and 'data-testid="stock-shortfall"' in html
        assert "CAB-25" in html and 'name="acknowledge"' in html
        assert Handover.objects.count() == 0
        posted = create(wo, data | {"acknowledge": "on"})
        assert Handover.objects.get().lines.get().qty == D(4)
        assert posted.redirect_chain

    def test_no_warning_when_the_warehouse_received_enough(
        self, org: Org, people: People, stocked: MaterialRequestLine
    ) -> None:
        wo = client_for(people.wo_mad)
        response = create(wo, form_data(wo, stocked, org, people), follow=False)
        assert response.status_code == 302

    def test_stock_received_elsewhere_does_not_count(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy, receive: Receive
    ) -> None:
        line = make_line("10")
        receive(buy((line, "10")), "10", at=org.worksite(org.hospital))
        wo = client_for(people.wo_mad)
        # the site receipt closed the request; use a new line asking for the warehouse
        other = make_line("10", sourcing="STOCK")
        warned = create(wo, form_data(wo, other, org, people), follow=False)
        assert warned.status_code == 200


# --- the page and the void --------------------------------------------------------------------


class TestHandoverPage:
    def test_a_warehouse_handover_can_be_voided_by_a_manager(
        self, org: Org, people: People, make_line: MakeLine, hand_over: HandOver
    ) -> None:
        stock = hand_over((make_line("10", sourcing="STOCK"), "4"))
        html = page(client_for(people.mm_mad), "detail", stock.pk)
        assert 'data-testid="void-form"' in html and stock.number in html
        assert field(html, "project") == f"{org.hospital.code} · {org.hospital.name}"
        assert 'data-testid="void-form"' not in page(client_for(people.wo_mad), "detail", stock.pk)
        assert 'data-testid="void-form"' not in page(
            client_for(people.site_manager), "detail", stock.pk
        )

    def test_a_site_handover_goes_with_its_receipt(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy, receive: Receive
    ) -> None:
        receipt = receive(buy((make_line("4"), "4")), "4", at=org.worksite(org.hospital))
        handover = receipt.direct_handover
        html = page(client_for(people.mm_mad), "detail", handover.pk)
        assert 'data-testid="void-form"' not in html and 'data-testid="direct-note"' in html
        assert reverse("receiving:detail", args=[receipt.pk]) in html

    def test_a_technician_cannot_read_it(
        self, org: Org, people: People, make_line: MakeLine, hand_over: HandOver
    ) -> None:
        stock = hand_over((make_line("10", sourcing="STOCK"), "4"))
        assert (
            client_for(people.tech_mad)
            .get(reverse("handovers:detail", args=[stock.pk]))
            .status_code
            == 403
        )
        assert (
            client_for(people.mm_sev).get(reverse("handovers:detail", args=[stock.pk])).status_code
            == 404
        )


class TestVoid:
    def test_voids_with_a_reason_and_shows_it(
        self, org: Org, people: People, make_line: MakeLine, hand_over: HandOver
    ) -> None:
        stock = hand_over((make_line("10", sourcing="STOCK"), "4"))
        response = client_for(people.mm_mad).post(
            reverse("handovers:void", args=[stock.pk]), {"reason": "Wrong project"}, follow=True
        )
        stock.refresh_from_db()
        assert stock.status == "VOIDED" and stock.void_reason == "Wrong project"
        assert any("voided" in message for message in messages_of(response))
        html = response.content.decode()
        assert 'data-testid="void-reason"' in html and "Wrong project" in html
        assert 'data-testid="void-form"' not in html

    def test_a_reason_is_required(
        self, org: Org, people: People, make_line: MakeLine, hand_over: HandOver
    ) -> None:
        stock = hand_over((make_line("10", sourcing="STOCK"), "4"))
        response = client_for(people.mm_mad).post(
            reverse("handovers:void", args=[stock.pk]), {"reason": " "}, follow=True
        )
        stock.refresh_from_db()
        assert stock.status == "POSTED"
        assert any("Give a reason" in message for message in messages_of(response))

    def test_the_void_dialog_states_what_it_does_and_comes_back_with_its_errors(
        self, org: Org, people: People, make_line: MakeLine, hand_over: HandOver
    ) -> None:
        stock = hand_over((make_line("10", sourcing="STOCK"), "4"))
        client = client_for(people.mm_mad)
        html = page(client, "detail", stock.pk)
        assert 'id="void-dialog"' in html and "The requests it served reopen." in html
        target = reverse("handovers:void", args=[stock.pk])
        again = client.post(target, {"reason": " "}, headers={"HX-Request": "true"})
        assert again.status_code == 422 and 'data-testid="void-form"' in again.content.decode()
        done = client.post(target, {"reason": "Wrong project"}, headers={"HX-Request": "true"})
        assert done.status_code == 204 and done["HX-Redirect"].endswith(f"/{stock.pk}/")

    def test_a_site_handover_is_refused_with_the_services_message(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy, receive: Receive
    ) -> None:
        receipt = receive(buy((make_line("4"), "4")), "4", at=org.worksite(org.hospital))
        response = client_for(people.mm_mad).post(
            reverse("handovers:void", args=[receipt.direct_handover.pk]),
            {"reason": "x"},
            follow=True,
        )
        assert any("goods receipt" in message for message in messages_of(response))
        receipt.direct_handover.refresh_from_db()
        assert receipt.direct_handover.status == "POSTED"

    def test_who_cannot_void_is_refused(
        self, org: Org, people: People, make_line: MakeLine, hand_over: HandOver
    ) -> None:
        stock = hand_over((make_line("10", sourcing="STOCK"), "4"))
        url = reverse("handovers:void", args=[stock.pk])
        assert client_for(people.wo_mad).post(url, {"reason": "x"}).status_code == 403
        assert client_for(people.site_manager).post(url, {"reason": "x"}).status_code == 403
        assert client_for(people.mm_sev).post(url, {"reason": "x"}).status_code == 404
        assert Location.objects.filter(pk=stock.source_location_id).exists()
