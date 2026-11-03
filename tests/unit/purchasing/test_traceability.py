"""The traceability screens (M6.5): the request ledger, the project's materials and the order
line's remaining and receivable quantities. Every number comes from a selector."""

import re
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import translation

from apps.access.context import AccessContext
from apps.access.exceptions import NotInScope
from apps.access.principals import SessionPrincipal
from apps.accounts.models import User
from apps.audit.recorder import record
from apps.catalog.models import Material
from apps.material_requests import selectors as request_selectors
from apps.material_requests import services as request_services
from apps.material_requests.models import MaterialRequestLine
from apps.projects.models import Project
from apps.purchasing import selectors
from tests.org import Org, People, client_for
from tests.support import every_key, read_all
from tests.unit.purchasing.conftest import Buy, FakeReceipts, MakeLine, allocation, only_line

pytestmark = pytest.mark.django_db

D = Decimal


@pytest.fixture(autouse=True)
def english() -> Iterator[None]:
    with translation.override("en"):
        yield


def ctx(user: object) -> AccessContext:
    return AccessContext.for_user(user)  # type: ignore[arg-type]


def field(html: str, name: str) -> str:
    """The text of the ``data-field`` element (its quantity and unit)."""
    match = re.search(rf'data-field="{name}">\s*([^<]*?)\s*<', html)
    assert match is not None, name
    return match.group(1)


def get(user: User, name: str, *args: object) -> tuple[Client, object]:
    client = client_for(user, HTTP_ACCEPT_LANGUAGE="en")
    return client, client.get(reverse(name, args=args))


class TestRequestTrims:
    def test_a_closure_is_explained_to_who_follows_the_request_up(
        self,
        people: People,
        mm: SessionPrincipal,
        buy: Buy,
        make_line: MakeLine,
        received: FakeReceipts,
    ) -> None:
        line = make_line("10")
        order = buy((line, "10"))
        received.receive(only_line(order), "4", {allocation(order, line): "4"})
        request_services.close_line_short(mm, line.pk, reason="Supplier short")
        found = request_selectors.request(ctx(people.mm_mad), line.request_id)
        (trim,) = selectors.request_trims(ctx(people.mm_mad), found)[1]
        assert (trim.before, trim.after, trim.line_no) == (D(10), D(4), 1)
        assert str(trim.reason) == "the request line was closed short"
        # the requester sees the request but not the timeline
        assert selectors.request_trims(ctx(people.tech_mad), found) == {}

    def test_an_unknown_reason_has_no_label(
        self, org: Org, people: People, mm: SessionPrincipal, buy: Buy, make_line: MakeLine
    ) -> None:
        line = make_line("10")
        record(
            mm.audit_identity,
            "purchase_allocation.trimmed",
            obj=allocation(buy((line, "10")), line),
            branch_id=org.a_mad.pk,
            data={"request_id": line.request_id, "request_line": 1, "from": "10", "to": "9"}
            | {"reason": "OTHER"},
        )
        found = request_selectors.request(ctx(people.mm_mad), line.request_id)
        assert selectors.request_trims(ctx(people.mm_mad), found)[1][0].reason == ""


class TestLedgerPage:
    def test_the_line_ledger_and_its_closure(
        self,
        people: People,
        mm: SessionPrincipal,
        buy: Buy,
        make_line: MakeLine,
        received: FakeReceipts,
    ) -> None:
        line = make_line("10")
        order = buy((line, "10"))
        received.receive(only_line(order), "4", {allocation(order, line): "4"})
        _, page = get(people.mm_mad, "requests:ledger", line.request_id)
        html = page.content.decode()  # type: ignore[attr-defined]
        assert page.status_code == 200  # type: ignore[attr-defined]
        shown = {name: field(html, name) for name in ("approved", "allocated", "received")}
        assert shown == {"approved": "10 M", "allocated": "10 M", "received": "4 M"}
        assert field(html, "pending") == "6 M"
        assert field(html, "outstanding") == "10 M"
        assert field(html, "awaiting") == "4 M"
        request_services.close_line_short(mm, line.pk, reason="Supplier short")
        html = get(people.mm_mad, "requests:ledger", line.request_id)[1].content.decode()  # type: ignore[attr-defined]
        assert 'data-testid="line-closure"' in html
        assert "Supplier short" in html
        assert 'data-testid="trim"' in html
        assert "the request line was closed short" in html

    def test_the_requester_reads_numbers_without_the_timeline(
        self,
        people: People,
        mm: SessionPrincipal,
        buy: Buy,
        make_line: MakeLine,
    ) -> None:
        line = make_line("10")
        buy((line, "10"))
        request_services.close_line_short(mm, line.pk, reason="Supplier short")
        _, page = get(people.tech_mad, "requests:ledger", line.request_id)
        html = page.content.decode()  # type: ignore[attr-defined]
        assert page.status_code == 200  # type: ignore[attr-defined]
        assert 'data-testid="line-closure"' in html
        assert 'data-testid="trim"' not in html

    def test_out_of_scope_is_not_found(self, people: People, make_line: MakeLine) -> None:
        line = make_line("10")
        assert get(people.mm_sev, "requests:ledger", line.request_id)[1].status_code == 404  # type: ignore[attr-defined]
        assert get(people.partner, "requests:ledger", line.request_id)[1].status_code == 404  # type: ignore[attr-defined]


class TestProjectMaterials:
    def test_open_lines_are_summed_per_material(
        self,
        org: Org,
        people: People,
        mm: SessionPrincipal,
        buy: Buy,
        make_line: MakeLine,
        received: FakeReceipts,
        delivered: dict[object, Decimal],
    ) -> None:
        first, second, gone = make_line("10"), make_line("6"), make_line("3")
        order = buy((first, "10"), (second, "6"))
        received.receive(only_line(order), "7", {allocation(order, first): "7"})
        delivered[first.pk] = D(2)
        request_services.close_line_short(mm, gone.pk, reason="Not needed")
        manager = ctx(people.mm_mad)
        project = request_selectors.project_for_overview(manager, org.hospital.pk)
        (row,) = materials(manager, project)
        assert (row.description, row.reference, row.unit_code, row.requests) == (
            "Cable 2,5",
            "CAB-25",
            "M",
            2,
        )
        assert [row.approved, row.received, row.pending, row.delivered] == q(16, 7, 9, 2)
        assert [row.awaiting_handover, row.outstanding] == q(5, 14)

    def test_the_materials_come_in_alphabetical_order_a_few_at_a_time(
        self, org: Org, people: People, make_line: MakeLine, breaker: Material
    ) -> None:
        make_line("9", material=breaker)  # Diferencial 40A
        make_line("2")  # Cable 2,5
        free_text(make_line, "1", "anclajes")
        free_text(make_line, "4", "Anclajes")  # the same words: one row
        manager = ctx(people.mm_mad)
        project = request_selectors.project_for_overview(manager, org.hospital.pk)
        rows = materials(manager, project, size=1)
        assert [(row.description.casefold(), row.outstanding) for row in rows] == [
            ("anclajes", D(5)),
            ("cable 2,5", D(2)),
            ("diferencial 40a", D(9)),
        ]

    def test_a_stretch_takes_only_its_own_material_and_unit_pairs(
        self, org: Org, people: People, make_line: MakeLine, breaker: Material
    ) -> None:
        make_line("9", material=breaker)  # UD
        make_line("2")  # cable, M
        free_text(make_line, "1", "anclajes")  # M
        free_text(make_line, "4", "anclajes", unit=breaker.unit_id)  # the same words in UD
        manager = ctx(people.mm_mad)
        project = request_selectors.project_for_overview(manager, org.hospital.pk)
        source = request_selectors.ProjectMaterials(manager, project)
        keys = every_key(source.seek)
        assert len(keys) == 4  # the words in two units are two rows
        # the stretch after the first "anclajes" holds both units and the words again: the
        # first "anclajes" line matches the query but is not one of the stretch's pairs
        rows = [row for _, row in source.window(keys[1:])]
        assert [row.outstanding for row in rows] == [
            row.outstanding for row in materials(manager, project)[1:]
        ]

    def test_the_filters_narrow_in_the_database_and_on_the_ledger(
        self, org: Org, people: People, mm: SessionPrincipal, make_line: MakeLine, breaker: Material
    ) -> None:
        make_line("9", material=breaker)
        done = make_line("2")
        request_services.close_line_short(mm, done.pk, reason="Enough")  # no longer open
        free_text(make_line, "3", "Tacos de expansion")
        manager = ctx(people.mm_mad)
        project = request_selectors.project_for_overview(manager, org.hospital.pk)
        assert [row.reference for row in materials(manager, project, q="dif-40")] == ["DIF-40"]
        assert [row.description for row in materials(manager, project, q="TACOS")] == [
            "Tacos de expansion"
        ]
        assert [row.reference for row in materials(manager, project, q="cab")] == []
        assert len(materials(manager, project, outstanding_only=True)) == 2

    def test_scope(self, org: Org, people: People, make_line: MakeLine) -> None:
        make_line("5")
        with pytest.raises(NotInScope):
            request_selectors.project_for_overview(ctx(people.mm_sev), org.hospital.pk)
        # a requester sees the lines of the requests they may see, nobody else's
        tech = ctx(people.tech_mad)
        mine = request_selectors.project_for_overview(tech, org.hospital.pk)
        assert [row.approved for row in materials(tech, mine)] == q(5)

    def test_page(self, org: Org, people: People, make_line: MakeLine) -> None:
        make_line("5")
        _, page = get(people.mm_mad, "requests:project_materials", org.hospital.pk)
        html = page.content.decode()  # type: ignore[attr-defined]
        assert field(html, "outstanding") == "5 M"
        empty = get(people.mm_sev, "requests:project_materials", org.hospital.pk)[1]
        assert empty.status_code == 404  # type: ignore[attr-defined]
        quiet = get(people.mm_sev, "requests:project_materials", org.sev_works.pk)[1]
        assert quiet.status_code == 200  # type: ignore[attr-defined]
        assert "Nothing is outstanding" in quiet.content.decode()  # type: ignore[attr-defined]


class TestOrderLine:
    def test_remaining_and_receivable(
        self,
        people: People,
        buy: Buy,
        make_line: MakeLine,
        received: FakeReceipts,
    ) -> None:
        line = make_line("10")
        order = buy((line, "10"))
        received.receive(only_line(order), "4", {allocation(order, line): "4"})
        _, page = get(people.mm_mad, "purchasing:detail", order.pk)
        html = page.content.decode()  # type: ignore[attr-defined]
        assert field(html, "remaining") == "6 M"
        assert "Receivable" in html


def free_text(
    make_line: MakeLine, approved: str, text: str, unit: object = None
) -> MaterialRequestLine:
    """An approved line described in words, served from stock (not in the catalog)."""
    line = make_line(approved, sourcing=MaterialRequestLine.Sourcing.STOCK)
    found = MaterialRequestLine.objects.filter(pk=line.pk)
    found.update(material=None, free_text=text, unit_id=unit or line.unit_id)
    return line


def materials(
    of: AccessContext, project: Project, *, size: int = 2, **narrow: Any
) -> list[request_selectors.ProjectMaterial]:
    """Every row of the project's materials, read ``size`` materials at a time."""
    source = request_selectors.ProjectMaterials(of, project, **narrow)
    return read_all(source.seek, source.window, size)


def q(*values: int) -> list[Decimal]:
    return [Decimal(v) for v in values]
