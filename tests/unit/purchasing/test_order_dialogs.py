"""The order page's action dialogs (M8b.4): editing the draft, its lines and allocations,
discarding and cancelling, closing a line short, and the dispatch decisions.

The page draws one inline dialog per action (ids unique per line, allocation and dispatch);
a destructive one states what it does. Sent with htmx a form that cannot be sent comes back in
its open dialog (422), a saved one navigates (204 + ``HX-Redirect``); a plain POST keeps the
flash and the redirect.
"""

import re
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse

from apps.purchasing.models import PurchaseOrder
from tests.org import Org, People
from tests.org import client_for as signed_in
from tests.unit.purchasing.conftest import Buy, MakeLine, allocation, only_line

pytestmark = pytest.mark.django_db

HTMX = {"HX-Request": "true"}
Status = PurchaseOrder.Status


def client_for(user: Any) -> Client:
    return signed_in(user, HTTP_ACCEPT_LANGUAGE="en")


@pytest.fixture
def manager(people: People) -> Client:
    return client_for(people.mm_mad)


def detail(client: Client, order: PurchaseOrder) -> str:
    response = client.get(reverse("purchasing:detail", args=[order.pk]))
    assert response.status_code == 200
    return str(response.content.decode())


def send(client: Client, name: str, target: Any, data: dict[str, str] | None = None) -> Any:
    return client.post(reverse(f"purchasing:{name}", args=[target.pk]), data or {}, headers=HTMX)


class TestADraftPage:
    def test_offers_its_actions_as_dialogs_without_a_details_form(
        self, manager: Client, buy: Buy, make_line: MakeLine
    ) -> None:
        order = buy((make_line("10"), "10"), issue=False)
        page = detail(manager, order)
        for dialog_id in ("header-dialog", "discard-dialog", "line-edit-dialog-1"):
            assert f'id="{dialog_id}"' in page
        assert "line-remove-dialog-1" in page and "raise-dialog-" in page
        assert "<details" not in page.split("<main", 1)[1].split("</main>", 1)[0]
        # The discard dialog says what it does and opens with Cancel focused.
        assert "The quantities it reserves are released." in page
        assert re.search(r'autofocus[^>]*data-testid="dialog-cancel"', page)

    def test_no_id_repeats_on_a_page_with_many_lines(
        self, manager: Client, buy: Buy, make_line: MakeLine
    ) -> None:
        order = buy((make_line("10"), "10"), (make_line("4"), "4"), issue=False)
        ids = re.findall(r'\sid="([^"]+)"', detail(manager, order))
        assert not sorted({i for i in ids if ids.count(i) > 1})

    def test_the_issue_form_stays_and_the_header_dialog_is_sent_with_htmx(
        self, manager: Client, buy: Buy, make_line: MakeLine
    ) -> None:
        order = buy((make_line("10"), "10"), issue=False)
        assert 'data-testid="issue-form"' in detail(manager, order)
        response = send(manager, "update", order)
        assert response.status_code == 422
        assert 'data-testid="header-form"' in str(response.content.decode())


class TestSentFromTheDialog:
    def test_a_line_with_no_quantity_comes_back_with_its_ids(
        self, manager: Client, buy: Buy, make_line: MakeLine
    ) -> None:
        order = buy((make_line("10"), "10"), issue=False)
        response = send(manager, "update_line", only_line(order))
        assert response.status_code == 422
        page = str(response.content.decode())
        assert 'data-testid="line-form"' in page and "id_ordered_qty_line1" in page

    def test_a_line_is_saved_from_its_dialog(
        self, manager: Client, buy: Buy, make_line: MakeLine
    ) -> None:
        order = buy((make_line("10"), "10"), issue=False)
        line = only_line(order)
        response = send(manager, "update_line", line, {"ordered_qty": "10", "unit_price": "2"})
        assert response.status_code == 204
        assert response["HX-Redirect"] == reverse("purchasing:detail", args=[order.pk])
        line.refresh_from_db()
        assert str(line.unit_price) == "2.0000"

    def test_a_line_is_removed_from_its_dialog(
        self, manager: Client, buy: Buy, make_line: MakeLine
    ) -> None:
        order = buy((make_line("10"), "10"), issue=False)
        response = send(manager, "remove_line", only_line(order))
        assert response.status_code == 204
        assert not order.lines.exists()

    def test_removing_an_issued_line_is_refused_on_the_form(
        self, manager: Client, buy: Buy, make_line: MakeLine
    ) -> None:
        order = buy((make_line("10"), "10"))
        response = send(manager, "remove_line", only_line(order))
        assert response.status_code == 422
        assert order.lines.exists()

    def test_an_allocation_is_raised_and_lowered(
        self, manager: Client, buy: Buy, make_line: MakeLine
    ) -> None:
        line = make_line("10")
        order = buy((line, "10"), issue=False)
        found = allocation(order, line)
        assert send(manager, "unallocate", found, {"qty": "4"}).status_code == 204
        found.refresh_from_db()
        assert found.qty == 4
        assert send(manager, "increase_allocation", found, {"qty": "6"}).status_code == 204
        found.refresh_from_db()
        assert found.qty == 6
        assert send(manager, "unallocate", found).status_code == 422  # no quantity

    def test_a_raise_above_what_the_line_can_take_is_refused_on_the_form(
        self, manager: Client, buy: Buy, make_line: MakeLine
    ) -> None:
        line = make_line("10")
        order = buy((line, "4"), issue=False)
        found = allocation(order, line)
        response = send(manager, "increase_allocation", found, {"qty": "99"})
        assert response.status_code == 422
        assert 'data-testid="allocation-raise-form"' in str(response.content.decode())

    def test_nothing_ticked_comes_back_in_the_add_lines_dialog(
        self, manager: Client, buy: Buy, make_line: MakeLine
    ) -> None:
        order = buy(issue=False)
        make_line("5")
        response = send(manager, "add_lines", order)
        assert response.status_code == 422
        page = str(response.content.decode())
        assert 'data-testid="add-lines-form"' in page and "data-error-summary" in page

    def test_the_draft_is_discarded_and_the_issued_order_cancelled(
        self, manager: Client, buy: Buy, make_line: MakeLine
    ) -> None:
        draft = buy((make_line("10"), "10"), issue=False)
        assert send(manager, "cancel", draft, {"reason": "Wrong supplier"}).status_code == 204
        draft.refresh_from_db()
        assert draft.status == Status.CANCELLED
        issued = buy((make_line("5"), "5"))
        assert send(manager, "cancel", issued).status_code == 422  # a reason is required
        assert send(manager, "cancel", issued, {"reason": "Not needed"}).status_code == 204

    def test_closing_a_line_short_without_a_reason_comes_back_in_its_dialog(
        self, manager: Client, buy: Buy, make_line: MakeLine
    ) -> None:
        order = buy((make_line("10"), "10"))
        response = send(manager, "close_line", only_line(order))
        assert response.status_code == 422
        assert "id_reason_close1" in str(response.content.decode())

    def test_a_plain_post_still_flashes_and_redirects(
        self, manager: Client, buy: Buy, make_line: MakeLine, org: Org
    ) -> None:
        order = buy((make_line("10"), "10"), issue=False)
        response = manager.post(
            reverse("purchasing:update_line", args=[only_line(order).pk]),
            {"ordered_qty": "10"},
            follow=True,
        )
        assert any("saved" in str(m) for m in response.context["messages"])
