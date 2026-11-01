"""The purchasing screens (M5.2): worklist, builder, order lists, order page, amendment,
printable page, and the actions behind them.

Each action is a POST-only URL that calls its service and redirects back to the order page
with a flash message; pages are read like a user does, and the ``data-testid`` selectors the
e2e layer will use are checked present. Prices reach only ``price.view`` holders (§8.2).
"""

import re
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.access.principals import SessionPrincipal, SystemPrincipal
from apps.audit.models import AuditEvent
from apps.core.locking import RetryExhausted
from apps.material_requests import services as request_services
from apps.material_requests.models import MaterialRequestLine
from apps.notifications.models import EmailOutbox, Outcome
from apps.notifications.models import Status as MailStatus
from apps.notifications.services import Claim, claim_next, record_outcome
from apps.purchasing import services
from apps.purchasing.models import PurchaseAllocation, PurchaseOrder, PurchaseOrderLine
from tests.org import Org, People
from tests.org import client_for as signed_in
from tests.outbox import make_dispatch
from tests.unit.purchasing.conftest import Buy, FakeReceipts, MakeLine, allocation, only_line

pytestmark = pytest.mark.django_db

D = Decimal
Status = PurchaseOrder.Status


def client_for(user: Any) -> Client:
    return signed_in(user, HTTP_ACCEPT_LANGUAGE="en")


def page(client: Client, name: str, *args: Any, **query: Any) -> str:
    response = client.get(reverse(f"purchasing:{name}", args=args), query)
    assert response.status_code == 200
    return str(response.content.decode())


def post(client: Client, name: str, *args: Any, data: dict[str, Any] | None = None) -> Any:
    return client.post(reverse(f"purchasing:{name}", args=args), data or {}, follow=True)


def messages_of(response: Any) -> list[str]:
    return [str(message) for message in response.context["messages"]]


def hidden(html: str, name: str, form: str = "") -> str:
    """The value of a hidden input, as the page rendered it (within the form ``form``)."""
    if form:
        html = html.split(f'data-testid="{form}"', 1)[1].split("</form>", 1)[0]
    found = re.search(rf'name="{name}"[^>]*value="([^"]*)"', html)
    assert found is not None, name
    return found.group(1)


def field(html: str, name: str) -> str:
    """The text of the first element marked ``data-field``, whitespace squeezed."""
    found = re.search(rf'data-field="{name}">([^<]*)<', html)
    assert found is not None, name
    return " ".join(found.group(1).split())


def refreshed(order: PurchaseOrder) -> PurchaseOrder:
    order.refresh_from_db()
    return order


@pytest.fixture
def manager(people: People) -> Client:
    return client_for(people.mm_mad)


# --- the worklist -------------------------------------------------------------------------


class TestWorklist:
    def test_lists_the_lines_to_buy_by_material_with_a_tick_each(
        self, manager: Client, make_line: MakeLine
    ) -> None:
        first, second = make_line("10"), make_line("5")
        html = page(manager, "worklist")
        assert html.count('data-testid="worklist-material"') == 1
        assert html.count('data-testid="worklist-line"') == 2
        assert f'value="{first.pk}"' in html and f'value="{second.pk}"' in html
        assert 'data-field="to-purchase">15 ' in html

    def test_a_fully_reserved_line_is_listed_without_a_tick(
        self, manager: Client, make_line: MakeLine, buy: Buy
    ) -> None:
        line = make_line("10")
        buy((line, "10"), issue=False)
        html = page(manager, "worklist")
        assert "Reserved in drafts" in html and f'value="{line.pk}"' not in html

    def test_filters_by_project_and_priority(
        self, manager: Client, make_line: MakeLine, org: Org
    ) -> None:
        urgent = make_line("3", priority="URGENT")
        normal = make_line("4")
        html = page(manager, "worklist", priority="URGENT")
        assert f'value="{urgent.pk}"' in html and f'value="{normal.pk}"' not in html
        html = page(manager, "worklist", project=str(org.hospital.pk))
        assert f'value="{normal.pk}"' in html

    def test_flags_old_drafts_and_what_is_ordered_for_nobody(
        self,
        manager: Client,
        people: People,
        buy: Buy,
        make_line: MakeLine,
        mm: SessionPrincipal,
        received: FakeReceipts,
        org: Org,
    ) -> None:
        old = buy(issue=False)
        PurchaseOrder.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(15))
        line = make_line("10")
        order = buy((line, "10"))
        received.receive(only_line(order), "6", {allocation(order, line): "6"})
        request_services.close_line_short(mm, line.pk, reason="Enough")

        html = page(manager, "worklist")
        assert 'data-testid="stale-draft"' in html
        assert 'data-testid="unallocated-line"' in html and str(order.number) in html
        filtered = page(manager, "worklist", supplier=str(org.supplier.pk))
        assert 'data-testid="stale-draft"' in filtered

    def test_htmx_answers_the_fragment(self, manager: Client) -> None:
        response = manager.get(reverse("purchasing:worklist"), HTTP_HX_REQUEST="true")
        body = response.content.decode()
        assert 'id="worklist-results"' in body and "<html" not in body

    @pytest.mark.parametrize("who", ["wo_mad", "tech_mad", "site_manager", "partner"])
    def test_roles_without_po_manage_get_403(self, people: People, who: str) -> None:
        assert (
            client_for(getattr(people, who)).get(reverse("purchasing:worklist")).status_code == 403
        )


# --- the builder ----------------------------------------------------------------------------


def new_order_data(
    page_html: str, org: Org, line: MaterialRequestLine | None = None, **extra: Any
) -> dict[str, Any]:
    """What the builder's form would send for ``line`` (keys are read off the page)."""
    data = {
        "idempotency_key": hidden(page_html, "idempotency_key"),
        "items_key": hidden(page_html, "items_key"),
        "branch": str(org.a_mad.pk),
        "supplier": str(org.supplier.pk),
        "ship_to": str(org.warehouses["A Madrid"].pk),
    }
    if line is not None:
        data |= {f"pick_{line.pk}": "on", f"qty_{line.pk}": "4"}
    return data | extra


class TestBuilder:
    def test_the_ticked_lines_arrive_ticked_with_their_quantities(
        self, manager: Client, make_line: MakeLine
    ) -> None:
        line = make_line("10")
        html = page(manager, "new", line=str(line.pk))
        assert 'data-testid="new-order-line"' in html
        assert f'name="pick_{line.pk}"' in html and "checked" in html

    def test_a_line_another_draft_reserved_in_full_is_left_out(
        self, manager: Client, org: Org, buy: Buy, make_line: MakeLine
    ) -> None:
        line, other = make_line("10"), make_line("10")
        html = page(manager, "new", line=[str(line.pk), str(other.pk)])
        buy((other, "10"), issue=False)  # meanwhile, another buyer reserves all of it
        alone = manager.get(reverse("purchasing:new"), {"line": str(other.pk)})
        assert alone.status_code == 302  # nothing left to purchase: back to the worklist
        data = new_order_data(html, org, line)
        data |= {f"pick_{other.pk}": "on", f"qty_{other.pk}": "4"}
        response = manager.post(reverse("purchasing:create"), data, follow=True)
        assert "1 ticked line had nothing left to purchase and was left out." in messages_of(
            response
        )
        assert "Draft order created with 1 request lines." in messages_of(response)

    def test_an_empty_builder_has_no_lines(self, manager: Client) -> None:
        html = page(manager, "new")
        assert 'data-testid="new-order-form"' in html and "new-order-line" not in html

    def test_lines_with_nothing_left_to_buy_go_back_to_the_worklist(
        self, manager: Client, make_line: MakeLine
    ) -> None:
        line = make_line("10", sourcing="STOCK")
        response = manager.get(reverse("purchasing:new"), {"line": str(line.pk)}, follow=True)
        assert response.redirect_chain[-1][0] == reverse("purchasing:worklist")
        assert "nothing left to purchase" in " ".join(messages_of(response))

    def test_a_malformed_id_is_ignored(self, manager: Client) -> None:
        assert manager.get(reverse("purchasing:new"), {"line": "nonsense"}).status_code == 200

    def test_creates_the_draft_with_the_lines(
        self, manager: Client, make_line: MakeLine, org: Org
    ) -> None:
        line = make_line("10")
        html = page(manager, "new", line=str(line.pk))
        response = manager.post(reverse("purchasing:create"), new_order_data(html, org, line))
        order = PurchaseOrder.objects.get()
        assert response.status_code == 302
        assert response["Location"] == reverse("purchasing:detail", args=[order.pk])
        assert order.status == Status.DRAFT
        assert PurchaseAllocation.objects.get().qty == D(4)

    def test_a_double_tap_creates_one_draft_and_one_allocation(
        self, manager: Client, make_line: MakeLine, org: Org
    ) -> None:
        line = make_line("10")
        data = new_order_data(page(manager, "new", line=str(line.pk)), org, line)
        manager.post(reverse("purchasing:create"), data)
        manager.post(reverse("purchasing:create"), data)
        assert PurchaseOrder.objects.count() == 1
        assert PurchaseAllocation.objects.get().qty == D(4)

    def test_a_ticked_line_with_nothing_left_to_buy_is_reported_as_left_out(
        self, manager: Client, make_line: MakeLine, org: Org
    ) -> None:
        line, stock = make_line("10"), make_line("10", sourcing="STOCK")
        data = new_order_data(page(manager, "new", line=str(line.pk)), org, line)
        data[f"pick_{stock.pk}"] = "on"
        response = manager.post(reverse("purchasing:create"), data, follow=True)
        assert "1 ticked line had nothing left to purchase and was left out." in messages_of(
            response
        )
        assert PurchaseAllocation.objects.get().request_line == line

    def test_an_empty_draft_can_be_created(self, manager: Client, org: Org) -> None:
        html = page(manager, "new")
        response = manager.post(
            reverse("purchasing:create"), new_order_data(html, org), follow=True
        )
        assert PurchaseOrder.objects.get().lines.count() == 0
        assert "Draft order created." in messages_of(response)

    def test_a_refused_line_keeps_the_draft_and_says_why(
        self, manager: Client, make_line: MakeLine, org: Org
    ) -> None:
        line = make_line("10")
        data = new_order_data(page(manager, "new", line=str(line.pk)), org, line)
        data[f"qty_{line.pk}"] = "11"  # more than is left to buy
        response = manager.post(reverse("purchasing:create"), data, follow=True)
        assert PurchaseOrder.objects.count() == 1 and not PurchaseAllocation.objects.exists()
        assert messages_of(response)  # the refusal, flashed on the draft's page

    def test_a_destination_of_another_branch_is_refused(
        self, manager: Client, make_line: MakeLine, org: Org
    ) -> None:
        line = make_line("10")
        data = new_order_data(page(manager, "new", line=str(line.pk)), org, line)
        data["ship_to"] = str(org.warehouses["A Sevilla"].pk)
        response = manager.post(reverse("purchasing:create"), data)
        assert response.status_code == 200 and not PurchaseOrder.objects.exists()

    def test_lines_bought_by_another_branch_are_refused(
        self, make_line: MakeLine, org: Org, people: People
    ) -> None:
        admin = client_for(people.group_admin)
        line = make_line("10", project=org.sev_works)  # A-SEV buys for it, not A-MAD
        data = new_order_data(page(admin, "new", line=str(line.pk)), org, line)
        response = admin.post(reverse("purchasing:create"), data)
        assert response.status_code == 200 and not PurchaseOrder.objects.exists()
        assert "is bought for by another branch" in response.content.decode()

    def test_a_line_out_of_scope_is_ignored(
        self, manager: Client, make_line: MakeLine, org: Org
    ) -> None:
        line = make_line("10", project=org.sev_works)  # not in the manager's scope
        data = new_order_data(page(manager, "new"), org, line)
        manager.post(reverse("purchasing:create"), data)
        assert not PurchaseAllocation.objects.exists()

    def test_a_service_error_comes_back_on_the_form(
        self, manager: Client, make_line: MakeLine, org: Org
    ) -> None:
        line = make_line("10")
        data = new_order_data(page(manager, "new", line=str(line.pk)), org, line)
        org.supplier.is_active = False
        org.supplier.save()
        response = manager.post(reverse("purchasing:create"), data)
        assert response.status_code == 200 and not PurchaseOrder.objects.exists()

    def test_a_tampered_key_is_refused(
        self, manager: Client, make_line: MakeLine, org: Org
    ) -> None:
        line = make_line("10")
        data = new_order_data(page(manager, "new", line=str(line.pk)), org, line)
        data["idempotency_key"] = "forged"
        assert manager.post(reverse("purchasing:create"), data).status_code == 400

    def test_a_conflict_answers_409(
        self, manager: Client, make_line: MakeLine, org: Org, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        line = make_line("10")
        data = new_order_data(page(manager, "new", line=str(line.pk)), org, line)

        def fail(*args: Any, **kwargs: Any) -> None:
            raise RetryExhausted

        monkeypatch.setattr(services, "create_po", fail)
        assert manager.post(reverse("purchasing:create"), data).status_code == 409


# --- the order lists -------------------------------------------------------------------------


class TestOrderList:
    def test_defaults_to_the_orders_still_to_receive(
        self, manager: Client, buy: Buy, make_line: MakeLine, received: FakeReceipts
    ) -> None:
        issued = buy((make_line("10"), "10"))
        draft = buy(issue=False)
        html = page(manager, "orders")
        assert str(issued.number) in html
        assert html.count('data-testid="order-row"') == 1
        assert "Not received" in html
        everything = page(manager, "orders", status="")
        assert everything.count('data-testid="order-row"') == 2
        assert page(manager, "orders", status="DRAFT").count('data-testid="order-row"') == 1
        assert str(draft.pk) not in html

    def test_shows_how_much_has_arrived(
        self, manager: Client, buy: Buy, make_line: MakeLine, received: FakeReceipts
    ) -> None:
        line = make_line("10")
        order = buy((line, "10"))
        received.receive(only_line(order), "4", {allocation(order, line): "4"})
        assert "Partly received" in page(manager, "orders")
        received.receive(only_line(order), "6", {allocation(order, line): "6"})
        assert "Received" in page(manager, "orders")

    def test_paging_keeps_the_all_statuses_choice(
        self, manager: Client, buy: Buy, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("apps.purchasing.selectors.PAGE_SIZE", 1)
        buy(issue=False)
        buy(issue=False)
        html = page(manager, "orders", status="")
        assert "?page=2&amp;status=" in html or "?page=2&status=" in html

    def test_only_issued_orders_show_receipt_progress(
        self, manager: Client, buy: Buy, make_line: MakeLine
    ) -> None:
        buy(issue=False)
        assert "Not received" not in page(manager, "orders", status="")
        buy((make_line("10"), "10"))
        assert "Not received" in page(manager, "orders", status="")

    def test_filters_by_supplier(self, manager: Client, buy: Buy, org: Org) -> None:
        buy(issue=False)
        found = page(manager, "orders", status="", supplier=str(org.supplier.pk))
        assert found.count('data-testid="order-row"') == 1

    def test_a_warehouse_operator_reads_it_without_the_manager_tools(
        self, people: People, buy: Buy, make_line: MakeLine
    ) -> None:
        buy((make_line("10"), "10"))
        html = page(client_for(people.wo_mad), "orders")
        assert 'data-testid="order-row"' in html and "To purchase" not in html.split("<main")[1]

    def test_htmx_answers_the_fragment(self, manager: Client) -> None:
        response = manager.get(reverse("purchasing:orders"), HTTP_HX_REQUEST="true")
        assert 'id="order-results"' in response.content.decode()

    @pytest.mark.parametrize("who", ["tech_mad", "site_manager", "partner"])
    def test_roles_without_po_view_get_403(self, people: People, who: str) -> None:
        assert client_for(getattr(people, who)).get(reverse("purchasing:orders")).status_code == 403


# --- a draft's page ------------------------------------------------------------------------


@pytest.fixture
def draft(buy: Buy, make_line: MakeLine) -> PurchaseOrder:
    return buy((make_line("10"), "6"), issue=False)


class TestDraftPage:
    def test_shows_the_lines_the_allocations_and_the_builders_tools(
        self, manager: Client, draft: PurchaseOrder
    ) -> None:
        html = page(manager, "detail", draft.pk)
        for selector in ("order-line", "allocation", "header-form", "issue-form", "discard-form"):
            assert f'data-testid="{selector}"' in html
        assert field(html, "allocated") == "6 M" and field(html, "unreserved") == "0 M"
        assert "Draft order" in html and "Printable page" not in html

    def test_offers_the_lines_still_to_buy(
        self, manager: Client, draft: PurchaseOrder, make_line: MakeLine
    ) -> None:
        other = make_line("7")
        html = page(manager, "detail", draft.pk)
        assert 'data-testid="add-lines-form"' in html and f"pick_{other.pk}" in html

    def test_adds_request_lines(
        self, manager: Client, draft: PurchaseOrder, make_line: MakeLine
    ) -> None:
        other = make_line("7")
        html = page(manager, "detail", draft.pk)
        response = post(
            manager,
            "add_lines",
            draft.pk,
            data={
                "idempotency_key": hidden(html, "idempotency_key", "add-lines-form"),
                f"pick_{other.pk}": "on",
                f"qty_{other.pk}": "3",
            },
        )
        assert "Request lines added." in messages_of(response)
        assert PurchaseAllocation.objects.filter(request_line=other, qty=D(3)).exists()
        assert only_line(draft).ordered_qty == D(9)

    def test_adding_nothing_is_refused(self, manager: Client, draft: PurchaseOrder) -> None:
        html = page(manager, "detail", draft.pk)
        response = post(
            manager,
            "add_lines",
            draft.pk,
            data={"idempotency_key": hidden(html, "idempotency_key")},
        )
        assert "Choose at least one request line." in " ".join(messages_of(response))

    def test_a_manager_without_price_view_cannot_see_price_fields(
        self, people: People, draft: PurchaseOrder
    ) -> None:
        # Only group administrators and material managers hold po.manage, and both hold
        # price.view: the price fields are always offered to who edits a line.
        html = page(client_for(people.mm_mad), "detail", draft.pk)
        assert 'name="unit_price"' in html and 'name="discount_pct"' in html

    def test_edits_the_header(self, manager: Client, draft: PurchaseOrder, org: Org) -> None:
        response = post(
            manager,
            "update",
            draft.pk,
            data={
                "supplier": str(org.supplier.pk),
                "ship_to": str(org.warehouses["A Madrid"].pk),
                "expected_date": "2030-05-01",
                "supplier_ref": "REF-1",
                "notes": "Deliver in the morning",
            },
        )
        assert "Draft saved." in messages_of(response)
        saved = refreshed(draft)
        assert (str(saved.expected_date), saved.supplier_ref) == ("2030-05-01", "REF-1")

    def test_a_bad_header_is_refused(self, manager: Client, draft: PurchaseOrder) -> None:
        response = post(manager, "update", draft.pk, data={"supplier": "nonsense"})
        assert response.redirect_chain[-1][0] == reverse("purchasing:detail", args=[draft.pk])
        assert messages_of(response)

    def test_edits_a_line_with_its_price(self, manager: Client, draft: PurchaseOrder) -> None:
        line = only_line(draft)
        response = post(
            manager,
            "update_line",
            line.pk,
            data={"ordered_qty": "8", "unit_price": "2.5", "discount_pct": "10"},
        )
        assert "Line 1 saved." in messages_of(response)
        saved = only_line(draft)
        assert (saved.ordered_qty, saved.unit_price, saved.discount_pct) == (D(8), D("2.5"), D(10))
        assert "2.5" in page(manager, "detail", draft.pk)

    def test_a_bad_line_quantity_is_refused(self, manager: Client, draft: PurchaseOrder) -> None:
        response = post(manager, "update_line", only_line(draft).pk, data={"ordered_qty": "0"})
        assert messages_of(response) and only_line(draft).ordered_qty == D(6)

    def test_removes_a_line(self, manager: Client, draft: PurchaseOrder) -> None:
        response = post(manager, "remove_line", only_line(draft).pk)
        assert "Line 1 removed." in messages_of(response)
        assert not draft.lines.exists()

    def test_raises_and_lowers_an_allocation(
        self, manager: Client, draft: PurchaseOrder, make_line: MakeLine
    ) -> None:
        alloc = PurchaseAllocation.objects.get()
        lowered = post(manager, "unallocate", alloc.pk, data={"qty": "2"})
        assert "Allocation lowered." in messages_of(lowered)
        assert PurchaseAllocation.objects.get().qty == D(2)
        raised = post(manager, "increase_allocation", alloc.pk, data={"qty": "5"})
        assert "Allocation raised." in messages_of(raised)
        assert PurchaseAllocation.objects.get().qty == D(5)

    def test_a_bad_allocation_quantity_is_refused(
        self, manager: Client, draft: PurchaseOrder
    ) -> None:
        alloc = PurchaseAllocation.objects.get()
        response = post(manager, "increase_allocation", alloc.pk, data={"qty": "x"})
        assert messages_of(response) and PurchaseAllocation.objects.get().qty == D(6)
        response = post(manager, "increase_allocation", alloc.pk, data={"qty": "1"})
        assert messages_of(response) and PurchaseAllocation.objects.get().qty == D(6)

    def test_discards_the_draft(self, manager: Client, draft: PurchaseOrder) -> None:
        response = post(manager, "cancel", draft.pk, data={"reason": "wrong supplier"})
        assert "Draft discarded." in messages_of(response)
        assert refreshed(draft).status == Status.CANCELLED


class TestIssue:
    def test_issues_the_order_and_numbers_it(
        self, manager: Client, draft: PurchaseOrder, people: People
    ) -> None:
        html = page(manager, "detail", draft.pk)
        response = post(
            manager,
            "issue",
            draft.pk,
            data={"idempotency_key": hidden(html, "idempotency_key"), "confirm": "on"},
        )
        issued = refreshed(draft)
        assert issued.status == Status.ISSUED and issued.number
        assert f"Order {issued.number} issued." in messages_of(response)
        again = page(manager, "detail", draft.pk)
        assert 'data-testid="order-revision"' in again and "Printable page" in again

    def test_needs_the_box_ticked(self, manager: Client, draft: PurchaseOrder) -> None:
        response = post(manager, "issue", draft.pk, data={"idempotency_key": "x"})
        assert refreshed(draft).status == Status.DRAFT
        assert "Tick the box to issue the order." in " ".join(messages_of(response))

    def test_an_empty_order_is_refused(self, manager: Client, buy: Buy) -> None:
        empty = buy(issue=False)
        html = page(manager, "detail", empty.pk)
        response = post(
            manager,
            "issue",
            empty.pk,
            data={"idempotency_key": hidden(html, "idempotency_key"), "confirm": "on"},
        )
        assert "Add at least one line before issuing." in messages_of(response)

    def test_a_forged_key_is_refused(self, manager: Client, draft: PurchaseOrder) -> None:
        response = manager.post(
            reverse("purchasing:issue", args=[draft.pk]),
            {"idempotency_key": "forged", "confirm": "on"},
        )
        assert response.status_code == 400 and refreshed(draft).status == Status.DRAFT


# --- an issued order ----------------------------------------------------------------------


@pytest.fixture
def issued(buy: Buy, make_line: MakeLine) -> PurchaseOrder:
    return buy((make_line("10"), "10"))


class TestIssuedPage:
    def test_shows_receipt_progress_and_the_amendment_tools(
        self, manager: Client, issued: PurchaseOrder
    ) -> None:
        html = page(manager, "detail", issued.pk)
        assert str(issued.number) in html
        for field in ("ordered", "received", "pending", "unallocated"):
            assert f'data-field="{field}"' in html
        assert "Close the line short" in html and 'data-testid="cancel-form"' in html
        assert "Amend" in html and 'data-testid="issue-form"' not in html

    def test_a_warehouse_operator_sees_the_order_without_prices_or_tools(
        self, people: People, mm: SessionPrincipal, issued: PurchaseOrder
    ) -> None:
        # The order was issued with a price on its line (set while it was a draft).
        PurchaseAllocation.objects.get()
        PurchaseOrderLine.objects.filter(po=issued).update(unit_price=D("98765.4321"))
        manager_html = page(client_for(people.mm_mad), "detail", issued.pk)
        assert "98765.4321" in manager_html and 'data-testid="order-total"' in manager_html
        html = page(client_for(people.wo_mad), "detail", issued.pk)
        assert "98765" not in html and "order-total" not in html
        assert 'data-testid="cancel-form"' not in html and "Amend" not in html
        assert 'data-testid="order-history"' not in html and 'data-testid="allocation"' in html
        printed = page(client_for(people.wo_mad), "print", issued.pk)
        assert "98765" not in printed and 'data-testid="print-lines"' in printed

    def test_the_history_lists_revisions_and_trims(
        self, manager: Client, issued: PurchaseOrder, people: People
    ) -> None:
        response = post(
            manager,
            "amend",
            issued.pk,
            data=amend_data(manager, issued, reason="Supplier asked", notes="New note"),
        )
        assert "Order amended: revision 1." in messages_of(response)
        html = page(manager, "detail", issued.pk)
        assert "Amended" in html and "Supplier asked" in html and "Issued" in html

    def test_closes_a_line_short(self, manager: Client, issued: PurchaseOrder) -> None:
        line = only_line(issued)
        refused = post(manager, "close_line", line.pk, data={})
        assert only_line(issued).closed_short_at is None and messages_of(refused)
        done = post(manager, "close_line", line.pk, data={"reason": "Supplier short"})
        assert "Line 1 closed short." in messages_of(done)
        assert only_line(issued).closed_short_at is not None
        assert 'data-testid="line-closure"' in page(manager, "detail", issued.pk)

    def test_cancels_the_order(self, manager: Client, issued: PurchaseOrder) -> None:
        refused = post(manager, "cancel", issued.pk, data={})
        assert refreshed(issued).status == Status.ISSUED and messages_of(refused)
        done = post(manager, "cancel", issued.pk, data={"reason": "Wrong order"})
        assert "Order cancelled." in messages_of(done)
        assert refreshed(issued).status == Status.CANCELLED

    def test_a_received_order_cannot_be_cancelled(
        self, manager: Client, issued: PurchaseOrder, received: FakeReceipts
    ) -> None:
        received.receive(only_line(issued), "2", {PurchaseAllocation.objects.get(): "2"})
        response = post(manager, "cancel", issued.pk, data={"reason": "Too late"})
        assert refreshed(issued).status == Status.ISSUED
        assert "no longer be cancelled" in " ".join(messages_of(response))


def amend_data(client: Client, order: PurchaseOrder, **changes: Any) -> dict[str, Any]:
    """The amendment form as the page offers it, with ``changes`` applied."""
    html = page(client, "amendment", order.pk)
    data: dict[str, Any] = {}
    for tag in re.findall(r"<(?:input|textarea)[^>]*>", html):
        name = re.search(r'name="([^"]+)"', tag)
        value = re.search(r'value="([^"]*)"', tag)
        if name and name.group(1) != "csrfmiddlewaretoken":
            data[name.group(1)] = value.group(1) if value else ""
    textarea = re.search(r'<textarea[^>]*name="notes"[^>]*>(.*?)</textarea>', html, re.S)
    data["notes"] = textarea.group(1).strip() if textarea else ""
    return {key: value for key, value in data.items() if not key.startswith("add_pick_")} | changes


class TestAmendment:
    def test_the_form_offers_what_can_change(
        self, manager: Client, issued: PurchaseOrder, make_line: MakeLine
    ) -> None:
        other = make_line("4")
        html = page(manager, "amendment", issued.pk)
        assert 'data-testid="amend-form"' in html
        line, alloc = only_line(issued), PurchaseAllocation.objects.get()
        for name in (f"qty_{line.pk}", f"price_{line.pk}", f"alloc_{alloc.pk}", "reason"):
            assert f'name="{name}"' in html
        assert f"add_pick_{other.pk}" in html

    def test_a_draft_has_no_amendment(self, manager: Client, draft: PurchaseOrder) -> None:
        response = manager.get(reverse("purchasing:amendment", args=[draft.pk]), follow=True)
        assert response.redirect_chain[-1][0] == reverse("purchasing:detail", args=[draft.pk])
        assert "Only an issued order is amended." in messages_of(response)

    def test_amends_quantity_price_and_the_header(
        self, manager: Client, issued: PurchaseOrder
    ) -> None:
        line = only_line(issued)
        data = amend_data(
            manager,
            issued,
            reason="Quote changed",
            expected_date="2030-06-01",
            supplier_ref="Q-9",
            **{f"qty_{line.pk}": "12", f"price_{line.pk}": "3.5"},
        )
        response = post(manager, "amend", issued.pk, data=data)
        assert "Order amended: revision 1." in messages_of(response)
        amended = refreshed(issued)
        assert (amended.revision, str(amended.expected_date), amended.supplier_ref) == (
            1,
            "2030-06-01",
            "Q-9",
        )
        saved = only_line(issued)
        assert (saved.ordered_qty, saved.unit_price) == (D(12), D("3.5"))

    def test_trims_an_allocation(self, manager: Client, issued: PurchaseOrder) -> None:
        alloc = PurchaseAllocation.objects.get()
        data = amend_data(manager, issued, reason="Less", **{f"alloc_{alloc.pk}": "4"})
        post(manager, "amend", issued.pk, data=data)
        assert PurchaseAllocation.objects.get().qty == D(4)
        assert "Allocation trimmed" in page(manager, "detail", issued.pk)

    def test_adds_a_request_line(
        self, manager: Client, issued: PurchaseOrder, make_line: MakeLine
    ) -> None:
        other = make_line("4")
        data = amend_data(
            manager,
            issued,
            reason="One more",
            **{f"add_pick_{other.pk}": "on", f"add_qty_{other.pk}": "3"},
        )
        post(manager, "amend", issued.pk, data=data)
        assert PurchaseAllocation.objects.filter(request_line=other, qty=D(3)).exists()
        assert only_line(issued).ordered_qty == D(13)

    def test_a_reason_is_required(self, manager: Client, issued: PurchaseOrder) -> None:
        response = manager.post(
            reverse("purchasing:amend", args=[issued.pk]), amend_data(manager, issued, reason="")
        )
        assert response.status_code == 200 and "Give a reason." in response.content.decode()
        assert refreshed(issued).revision == 0

    def test_nothing_changed_is_said(self, manager: Client, issued: PurchaseOrder) -> None:
        response = post(
            manager, "amend", issued.pk, data=amend_data(manager, issued, reason="No change")
        )
        assert "Nothing changed" in " ".join(messages_of(response))
        assert refreshed(issued).revision == 0

    def test_a_service_refusal_comes_back_on_the_form(
        self, manager: Client, issued: PurchaseOrder, received: FakeReceipts
    ) -> None:
        line = only_line(issued)
        received.receive(line, "3", {PurchaseAllocation.objects.get(): "3"})
        data = amend_data(manager, issued, reason="Late", **{f"price_{line.pk}": "9"})
        response = manager.post(reverse("purchasing:amend", args=[issued.pk]), data)
        assert response.status_code == 200 and "goods received" in response.content.decode()
        assert refreshed(issued).revision == 0

    def test_a_forged_key_is_refused(self, manager: Client, issued: PurchaseOrder) -> None:
        data = amend_data(manager, issued, reason="x", idempotency_key="forged", notes="n")
        assert manager.post(reverse("purchasing:amend", args=[issued.pk]), data).status_code == 400

    def test_a_conflict_answers_409(
        self, manager: Client, issued: PurchaseOrder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        data = amend_data(manager, issued, reason="x", notes="n")

        def fail(*args: Any, **kwargs: Any) -> None:
            raise RetryExhausted

        monkeypatch.setattr(services, "amend", fail)
        assert manager.post(reverse("purchasing:amend", args=[issued.pk]), data).status_code == 409


# --- the printable page -------------------------------------------------------------------


class TestPrint:
    def test_prints_from_the_snapshots_not_the_live_data(
        self, manager: Client, issued: PurchaseOrder, org: Org
    ) -> None:
        name = org.supplier.name
        org.supplier.name = "Renamed Supplies"
        org.supplier.save()
        html = page(manager, "print", issued.pk)
        assert name in html and "Renamed Supplies" not in html
        assert str(issued.number) in html and "data-print" in html
        assert 'data-testid="print-lines"' in html

    def test_a_line_closed_short_prints_what_it_received(
        self,
        manager: Client,
        mm: SessionPrincipal,
        buy: Buy,
        make_line: MakeLine,
        breaker: Any,
        received: FakeReceipts,
    ) -> None:
        cable_line = make_line("10")
        order = buy((cable_line, "10"), (make_line("3", material=breaker), "3"))
        line = order.lines.get(line_no=1)
        received.receive(line, "4", {allocation(order, cable_line): "4"})
        services.close_line_short(mm, line.pk, reason="Out of stock")
        html = page(manager, "print", order.pk)
        assert html.count('data-testid="print-closed-short"') == 1
        assert re.search(r">\s*4 M\s*<span", html)

    def test_a_draft_is_not_printed(self, manager: Client, draft: PurchaseOrder) -> None:
        response = manager.get(reverse("purchasing:print", args=[draft.pk]), follow=True)
        assert response.redirect_chain[-1][0] == reverse("purchasing:detail", args=[draft.pk])
        assert "Issue the order to print it." in messages_of(response)


# --- scope ------------------------------------------------------------------------------------


class TestDispatches:
    """The supplier dispatches on the order page and a human's decision (M5.3)."""

    @pytest.fixture
    def ambiguous(self, buy: Buy, make_line: MakeLine) -> EmailOutbox:
        order = buy((make_line("10"), "10"))
        return make_dispatch(order, MailStatus.AMBIGUOUS)

    def test_the_panel_shows_each_dispatch_and_the_decision_to_take(
        self, manager: Client, ambiguous: EmailOutbox
    ) -> None:
        html = page(manager, "detail", ambiguous.source_po_id)
        assert 'data-testid="order-dispatches"' in html
        assert 'data-testid="dispatch-decision"' in html
        assert "Ambiguous" in html
        assert reverse("purchasing:confirm_dispatch", args=[ambiguous.pk]) in html
        assert reverse("purchasing:resend_dispatch", args=[ambiguous.pk]) in html

    def test_a_warehouse_operator_sees_the_status_without_the_decision(
        self, people: People, ambiguous: EmailOutbox
    ) -> None:
        html = page(client_for(people.wo_mad), "detail", ambiguous.source_po_id)
        assert "Ambiguous" in html
        assert reverse("purchasing:confirm_dispatch", args=[ambiguous.pk]) not in html

    def test_confirm(self, manager: Client, ambiguous: EmailOutbox) -> None:
        response = post(manager, "confirm_dispatch", ambiguous.pk, data={"note": "By phone"})
        assert messages_of(response) == ["Delivery to the supplier confirmed."]
        assert response.redirect_chain[-1][0] == reverse(
            "purchasing:detail", args=[ambiguous.source_po_id]
        )
        assert EmailOutbox.objects.get(pk=ambiguous.pk).status == MailStatus.SENT
        event = AuditEvent.objects.get(action="purchase_order.dispatch_confirmed")
        assert event.data["note"] == "By phone"

    def test_resend(self, manager: Client, ambiguous: EmailOutbox) -> None:
        response = post(manager, "resend_dispatch", ambiguous.pk)
        assert messages_of(response) == ["The dispatch is queued to be sent again."]
        assert EmailOutbox.objects.get(pk=ambiguous.pk).status == MailStatus.QUEUED

    def test_a_refusal_is_flashed(self, manager: Client, ambiguous: EmailOutbox) -> None:
        post(manager, "confirm_dispatch", ambiguous.pk)
        response = post(manager, "confirm_dispatch", ambiguous.pk)
        assert messages_of(response) == [
            "Only a dispatch whose delivery is uncertain can be confirmed."
        ]

    def test_a_failed_dispatch_that_can_no_longer_go_offers_nothing(
        self, manager: Client, mm: SessionPrincipal, ambiguous: EmailOutbox
    ) -> None:
        EmailOutbox.objects.filter(pk=ambiguous.pk).update(status=MailStatus.FAILED)
        po_id = ambiguous.source_po_id
        assert po_id is not None
        services.amend(mm, po_id, reason="More", header=services.HeaderAmendment(notes="x"))
        html = page(manager, "detail", po_id)
        assert "Failed" in html
        assert reverse("purchasing:resend_dispatch", args=[ambiguous.pk]) not in html

    def test_with_the_switch_on_an_order_without_dispatch_says_so(
        self, manager: Client, buy: Buy, make_line: MakeLine, settings: Any
    ) -> None:
        order = buy((make_line("10"), "10"))
        assert 'data-testid="order-dispatches"' not in page(manager, "detail", order.pk)
        settings.PO_DISPATCH = True
        assert "Not sent by email" in page(manager, "detail", order.pk)

    def test_a_cancellation_notice_owed_is_listed(
        self, manager: Client, mm: SessionPrincipal, ambiguous: EmailOutbox
    ) -> None:
        services.cancel(mm, ambiguous.source_po_id, reason="Stop")  # type: ignore[arg-type]
        html = page(manager, "detail", ambiguous.source_po_id)
        assert 'data-testid="dispatch-cancellation"' not in html  # queued, so listed
        assert "Cancellation notice" in html

    def test_a_notice_that_waits_on_a_dispatch_being_sent_says_so_until_it_fails(
        self, manager: Client, mm: SessionPrincipal, buy: Buy, make_line: MakeLine
    ) -> None:
        order = buy((make_line("10"), "10"))
        sending = make_dispatch(order)
        system = SystemPrincipal()
        claimed = claim_next(system)
        assert isinstance(claimed, Claim) and claimed.message_id == sending.pk
        services.cancel(mm, order.pk, reason="Stop")
        assert 'data-testid="dispatch-cancellation"' in page(manager, "detail", order.pk)
        record_outcome(system, claimed.attempt_id, Outcome.FAILED, error="refused")
        assert 'data-testid="dispatch-cancellation"' not in page(manager, "detail", order.pk)


class TestScope:
    def test_another_branchs_order_is_not_found(
        self, people: People, issued: PurchaseOrder
    ) -> None:
        sevilla = client_for(people.mm_sev)
        for name in ("detail", "print", "amendment"):
            assert sevilla.get(reverse(f"purchasing:{name}", args=[issued.pk])).status_code == 404
        alloc = PurchaseAllocation.objects.get()
        line = only_line(issued)
        for name, target in (
            ("update", issued),
            ("cancel", issued),
            ("update_line", line),
            ("remove_line", line),
            ("close_line", line),
            ("increase_allocation", alloc),
            ("unallocate", alloc),
        ):
            assert sevilla.post(reverse(f"purchasing:{name}", args=[target.pk])).status_code == 404

    def test_a_warehouse_operator_is_forbidden_every_action(
        self, people: People, issued: PurchaseOrder
    ) -> None:
        operator = client_for(people.wo_mad)
        line, alloc = only_line(issued), PurchaseAllocation.objects.get()
        assert operator.get(reverse("purchasing:amendment", args=[issued.pk])).status_code == 403
        for name, target in (
            ("update", issued),
            ("issue", issued),
            ("amend", issued),
            ("add_lines", issued),
            ("cancel", issued),
            ("update_line", line),
            ("close_line", line),
            ("increase_allocation", alloc),
        ):
            assert operator.post(reverse(f"purchasing:{name}", args=[target.pk])).status_code == 403

    def test_audit_events_carry_the_actor(self, manager: Client, issued: PurchaseOrder) -> None:
        post(manager, "cancel", issued.pk, data={"reason": "Wrong order"})
        event = AuditEvent.objects.get(action="purchase_order.cancelled")
        assert event.actor_label.startswith("mm-mad") or "@" in event.actor_label
