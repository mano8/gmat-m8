"""Derived request-line quantities, predicates and eligibility (plan §4.5, §4.9, S9)."""

from decimal import Decimal
from uuid import UUID

import pytest
from django.utils import timezone

from apps.material_requests import ledger
from apps.material_requests.ledger import Flows
from apps.material_requests.models import MaterialRequest, MaterialRequestLine
from apps.organizations.deactivation import OpenDocument, open_documents
from tests.org import Org, People
from tests.unit.material_requests.conftest import MakeRequest

pytestmark = pytest.mark.django_db

Status = MaterialRequest.Status
D = Decimal


def only_line(request: MaterialRequest) -> MaterialRequestLine:
    return request.lines.select_related("request").get()


def close_short(line: MaterialRequestLine, people: People) -> None:
    MaterialRequestLine.objects.filter(pk=line.pk).update(
        closed_short_at=timezone.now(),
        closed_short_by=people.mm_mad,
        closed_short_source="LINE",
        close_reason="x",
    )


class TestFlows:
    def test_zero_without_sources(self) -> None:
        line_id = UUID(int=1)
        assert ledger.flows([line_id]) == {line_id: Flows()}
        assert ledger.flows([]) == {}

    def test_sources_add_up(self, flows_of: dict[UUID, Flows]) -> None:
        one, two = UUID(int=1), UUID(int=2)
        flows_of[one] = Flows(received=D(3), pending=D(2))
        unregister = ledger.register_flow_source(lambda ids: {one: Flows(delivered=D(1))})
        try:
            assert ledger.flows([one, two]) == {
                one: Flows(received=D(3), pending=D(2), delivered=D(1)),
                two: Flows(),
            }
        finally:
            unregister()
        unregister()  # a second call is harmless
        assert ledger.flows([one])[one] == Flows(received=D(3), pending=D(2))

    def test_a_source_registers_once(self) -> None:
        def source(ids: object) -> dict[UUID, Flows]:
            return {UUID(int=1): Flows(delivered=D(1))}

        unregister = ledger.register_flow_source(source)
        ledger.register_flow_source(source)
        try:
            assert ledger.flows([UUID(int=1)])[UUID(int=1)].delivered == 1
        finally:
            unregister()


class TestLineLedger:
    def test_an_open_line(self, make_request: MakeRequest, flows_of: dict[UUID, Flows]) -> None:
        line = only_line(make_request(Status.APPROVED, decided=True))  # A = 5
        flows_of[line.pk] = Flows(received=D(3), pending=D(1), reserved=D("0.5"), delivered=D(2))
        (entry,) = ledger.line_ledgers([line])
        assert entry.is_open and entry.approved == 5
        assert entry.floor == entry.committed == D("4.5")  # max(2, 3) + 1 + 0.5
        assert entry.awaiting_handover == 1  # min(3, 5) - 2
        assert entry.outstanding == 3
        assert entry.to_purchase == 0  # sourcing undecided
        line.sourcing = MaterialRequestLine.Sourcing.PURCHASE
        assert ledger.line_ledgers([line])[0].to_purchase == D("0.5")
        assert not entry.fully_delivered

    @pytest.mark.parametrize("status", [Status.SUBMITTED, Status.CLOSED])
    def test_a_line_of_a_request_not_approved_is_not_open(
        self, make_request: MakeRequest, flows_of: dict[UUID, Flows], status: str
    ) -> None:
        line = only_line(make_request(status, decided=True))
        line.sourcing = MaterialRequestLine.Sourcing.PURCHASE
        flows_of[line.pk] = Flows(received=D(2), delivered=D(1))
        (entry,) = ledger.line_ledgers([line])
        assert not entry.is_open
        quantities = (
            entry.committed,
            entry.awaiting_handover,
            entry.to_purchase,
            entry.outstanding,
        )
        assert set(quantities) == {D(0)}
        assert entry.floor == 2  # state-independent

    def test_live_and_open_predicates(self, make_request: MakeRequest, people: People) -> None:
        approved = only_line(make_request(Status.APPROVED, decided=True))
        pending = only_line(make_request(Status.SUBMITTED))
        zero = only_line(make_request(Status.APPROVED, decided=True))
        MaterialRequestLine.objects.filter(pk=zero.pk).update(approved_qty=0)
        zero.refresh_from_db()
        closed = only_line(make_request(Status.APPROVED, decided=True))
        close_short(closed, people)
        closed.refresh_from_db()
        assert [ledger.is_live(x) for x in (approved, pending, zero, closed)] == [
            True,
            False,
            False,
            False,
        ]
        assert set(MaterialRequestLine.objects.filter(ledger.open_q())) == {approved}
        live = MaterialRequestLine.objects.filter(ledger.live_q())
        assert set(live) == {approved}

    def test_a_closed_short_line_is_never_purchasable(
        self, make_request: MakeRequest, people: People
    ) -> None:
        line = only_line(make_request(Status.APPROVED, decided=True))
        MaterialRequestLine.objects.filter(pk=line.pk).update(sourcing="PURCHASE")
        lines = MaterialRequestLine.objects.select_related("request")
        assert [entry.line for entry in ledger.purchasable(lines)] == [line]
        close_short(line, people)
        assert ledger.purchasable(MaterialRequestLine.objects.select_related("request")) == []


class TestEligibility:
    def test_auto_close_needs_every_live_line_delivered(
        self, make_request: MakeRequest, flows_of: dict[UUID, Flows], people: People
    ) -> None:
        request = make_request(Status.APPROVED, lines=2, decided=True)
        first, second = request.lines.all()
        flows_of[first.pk] = Flows(delivered=D(5))
        assert not ledger.auto_close_eligible(ledger.line_ledgers(request.lines.all()))
        close_short(second, people)  # no longer live: it does not hold the close back
        assert ledger.auto_close_eligible(ledger.line_ledgers(request.lines.all()))

    def test_auto_reopen_needs_an_automatic_close(
        self, make_request: MakeRequest, flows_of: dict[UUID, Flows]
    ) -> None:
        request = make_request(Status.CLOSED, decided=True)  # close_mode AUTO
        entries = ledger.line_ledgers(request.lines.all())
        assert ledger.auto_reopen_eligible(request, entries)
        flows_of[request.lines.get().pk] = Flows(delivered=D(5))
        assert not ledger.auto_reopen_eligible(request, ledger.line_ledgers(request.lines.all()))
        request.close_mode = MaterialRequest.CloseMode.MANUAL
        assert not ledger.auto_reopen_eligible(request, entries)


class TestDeactivationProvider:
    def test_approved_requests_with_outstanding_quantity_block(
        self, make_request: MakeRequest, org: Org, flows_of: dict[UUID, Flows]
    ) -> None:
        approved = make_request(Status.APPROVED, decided=True)  # hospital: B-MAD, A-MAD buys
        make_request(Status.SUBMITTED)
        make_request(Status.CLOSED, decided=True)
        delivered = make_request(Status.APPROVED, decided=True)
        flows_of[delivered.lines.get().pk] = Flows(delivered=D(5))
        expected = [OpenDocument("material_request", str(approved.number), approved.pk)]
        assert open_documents([org.a_mad.pk]) == expected  # procurement branch
        assert open_documents([org.b_mad.pk]) == expected  # the project's branch
        assert open_documents([org.a_sev.pk]) == []
