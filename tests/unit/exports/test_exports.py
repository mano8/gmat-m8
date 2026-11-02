"""The CSV exports (plan M7.3): scope, prices (sentinel) and formula injection."""

import csv
import io
from collections.abc import Iterator
from typing import Any

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.access.context import AccessContext
from apps.access.roles import Capability
from apps.exports import selectors
from apps.material_requests.models import MaterialRequest
from apps.purchasing.models import PurchaseOrder
from tests.org import Org, People, Sentinel, build_sentinel_order, client_for
from tests.unit.purchasing.conftest import Buy, MakeLine
from tests.unit.receiving.conftest import Receive

pytestmark = pytest.mark.django_db

INJECTION = '=HYPERLINK("http://evil.example","click")'


def table(response: Any) -> list[dict[str, str]]:
    text = b"".join(response.streaming_content).decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


def download(user: Any, key: str) -> Any:
    return client_for(user).get(reverse("exports:download", kwargs={"key": key}))


class TestDownload:
    def test_it_is_an_attachment_with_a_bom_for_excel(self, org: Org, people: People) -> None:
        response = download(people.mm_mad, "requests")
        assert response["Content-Type"].startswith("text/csv")
        assert (response["Content-Disposition"] or "").startswith(
            'attachment; filename="gmat-m8-requests-'
        )
        assert next(iter(response.streaming_content)).startswith("﻿".encode())

    def test_the_index_lists_every_export(self, org: Org, people: People) -> None:
        html = client_for(people.mm_mad).get(reverse("exports:index")).content.decode()
        for key in selectors.BY_KEY:
            assert f'data-testid="export-{key}"' in html

    def test_an_unknown_export_is_not_found(self, org: Org, people: People) -> None:
        response = client_for(people.mm_mad).get("/exports/nothing.csv")
        assert response.status_code == 404

    @pytest.mark.parametrize("who", ["wo_mad", "tech_mad", "site_manager", "partner"])
    def test_roles_without_the_capability_are_refused(
        self, org: Org, people: People, who: str
    ) -> None:
        assert download(getattr(people, who), "orders").status_code == 403


class TestRequests:
    def test_scope_drafts_and_injection(
        self, org: Org, people: People, make_line: MakeLine
    ) -> None:
        line = make_line("10")
        MaterialRequest.objects.filter(pk=line.request_id).update(notes=INJECTION)
        draft = make_line("3", status=MaterialRequest.Status.DRAFT)
        rows = table(download(people.mm_mad, "requests"))
        assert [row["request"] for row in rows] == [line.request.number]
        assert str(draft.request.number) not in str(rows)
        assert rows[0]["notes"] == "'" + INJECTION
        assert rows[0]["requested_qty"] == "10.000"
        assert rows[0]["requested_by"] == people.tech_mad.email
        assert table(download(people.mm_sev, "requests")) == []

    def test_a_free_text_line(self, org: Org, people: People, make_line: MakeLine) -> None:
        line = make_line("2", sourcing="UNDECIDED")
        type(line).objects.filter(pk=line.pk).update(material=None, free_text="-cmd|' /C calc'!A0")
        (row,) = table(download(people.group_admin, "requests"))
        assert row["material_reference"] == ""
        assert row["description"] == "'-cmd|' /C calc'!A0"


class TestOrders:
    def test_a_priced_order_for_who_holds_price_view_only_in_scope(
        self, org: Org, people: People
    ) -> None:
        built = build_sentinel_order(org, people)
        mine = table(download(people.mm_mad, "orders"))
        assert [row["order"] for row in mine] == [built.order.number]
        assert mine[0]["unit_price"] == Sentinel.PRICE.value
        assert mine[0]["supplier"] == org.supplier.name
        assert table(download(people.mm_sev, "orders")) == []

    def test_the_price_never_reaches_another_branch(self, org: Org, people: People) -> None:
        build_sentinel_order(org, people)
        body = b"".join(download(people.mm_sev, "orders").streaming_content)
        assert Sentinel.PRICE.value.encode() not in body
        assert Sentinel.SUPPLIER_NAME.value.encode() not in body

    def test_prices_are_blank_where_price_view_does_not_reach(
        self, org: Org, people: People
    ) -> None:
        built = build_sentinel_order(org, people)

        class NoPriceOnOrders(AccessContext):
            def has(self, capability: str) -> bool:
                return True if capability == Capability.PRICE_VIEW else super().has(capability)

            def scope_for(self, capability: str) -> Any:
                if capability == Capability.PRICE_VIEW:
                    return super().scope_for("nothing.here")
                return super().scope_for(capability)

        ctx = NoPriceOnOrders.for_user(people.mm_mad)
        header, rows = selectors.orders(ctx)
        found = list(rows)
        assert "unit_price" in header
        assert len(found) == 1 and found[0][0] == built.order.number
        assert Sentinel.PRICE.value not in {str(v) for v in found[0]}
        assert found[0][-3:] == ["", "", ""]

    def test_without_price_view_there_is_no_price_column(self, org: Org, people: People) -> None:
        build_sentinel_order(org, people)

        class NoPrice(AccessContext):
            def has(self, capability: str) -> bool:
                return False if capability == Capability.PRICE_VIEW else super().has(capability)

        header, rows = selectors.orders(NoPrice.for_user(people.mm_mad))
        assert "unit_price" not in header
        assert Sentinel.PRICE.value not in str(list(rows))

    def test_a_snapshot_survives_a_later_change(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy
    ) -> None:
        order = buy((make_line("5"), "5"))
        order.lines.update(unit_price="3.5000")
        org.supplier.name = "Renamed Supplies"
        org.supplier.save()
        (row,) = table(download(people.mm_mad, "orders"))
        assert row["supplier"] == "Zyxwvut Sentinel Supplies"
        assert row["material_reference"] == "CAB-25"
        assert row["unit_price"] == "3.5000"

    def test_drafts_are_left_out_and_notes_injection_in_reasons_is_quoted(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy
    ) -> None:
        buy((make_line("5"), "5"), issue=False)
        issued = buy((make_line("4"), "4"))
        PurchaseOrder.objects.filter(pk=issued.pk).update(supplier_ref="+SUM(1)")
        issued.lines.update(close_reason=INJECTION, closed_short_at=timezone.now())
        (row,) = table(download(people.mm_mad, "orders"))
        assert row["order"] == issued.number
        assert row["supplier_reference"] == "'+SUM(1)"
        assert row["close_reason"] == "'" + INJECTION

    def test_a_line_without_snapshot_reads_the_catalog(self, org: Org, people: People) -> None:
        built = build_sentinel_order(org, people)
        (row,) = table(download(people.mm_mad, "orders"))
        assert row["material_reference"] == built.line.material.reference


class TestReceipts:
    def test_lines_scope_and_injection(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy, receive: Receive
    ) -> None:
        order = buy((make_line("8"), "8"))
        receipt = receive(order, "5", delivery_note_ref="=1+1")
        rows = table(download(people.mm_mad, "receipts"))
        assert [(r["receipt"], r["accepted_qty"]) for r in rows] == [(receipt.number, "5.000")]
        assert rows[0]["delivery_note"] == "'=1+1"
        assert rows[0]["order"] == order.number
        assert rows[0]["material_reference"] == "CAB-25"
        assert table(download(people.mm_sev, "receipts")) == []

    def test_a_voided_receipt_is_listed_with_its_reason(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy, receive: Receive
    ) -> None:
        from apps.receiving import services
        from tests.support import principal_for

        order = buy((make_line("8"), "8"))
        receipt = receive(order, "5")
        services.void_receipt(principal_for(people.mm_mad), receipt.pk, reason="@wrong")
        (row,) = table(download(people.mm_mad, "receipts"))
        assert (row["status"], row["void_reason"]) == ("VOIDED", "'@wrong")


def test_the_selector_builders_are_lazy(org: Org, people: People) -> None:
    ctx = AccessContext.for_user(people.mm_mad)
    for export in selectors.EXPORTS:
        header, rows = export.build(ctx)
        assert header and isinstance(rows, Iterator)
    assert selectors.can_export(ctx)
