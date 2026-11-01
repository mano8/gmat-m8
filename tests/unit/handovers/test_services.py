"""The rules of ``create_handover`` and ``void_handover`` (plan §4.5, §4.6 C7/C8/C10, §4.9, I5).

The worked examples are in ``test_examples``, who may call what in ``test_matrix``.
"""

from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.audit.models import AuditEvent
from apps.core import events
from apps.core.idempotency import IdempotencyConflict, issue_key
from apps.handovers import services
from apps.handovers.models import Handover
from apps.handovers.services import HandoverLineInput, Receiver
from apps.locations.models import Location
from apps.material_requests import services as request_services
from apps.material_requests.models import MaterialRequest, MaterialRequestLine
from apps.organizations.models import Branch
from apps.projects.models import Project
from apps.receiving import services as receiving
from tests.org import Org, People
from tests.support import error_codes, principal_for
from tests.unit.handovers.conftest import HandOver
from tests.unit.purchasing.conftest import Buy, MakeLine, flows
from tests.unit.receiving.conftest import Receive

pytestmark = pytest.mark.django_db

D = Decimal
STOCK = MaterialRequestLine.Sourcing.STOCK


@pytest.fixture
def published() -> Iterator[list[events.Event]]:
    found: list[events.Event] = []
    undo = [events.subscribe(name, found.append) for name in ("handover.posted", "handover.voided")]
    yield found
    for item in undo:
        item()


@pytest.fixture
def stock_line(make_line: MakeLine) -> MaterialRequestLine:
    """An approved line of 10 for the hospital, served from stock."""
    return make_line("10", sourcing=STOCK)


def codes(exc: Any) -> set[str]:
    return error_codes(exc)


def request_of(line: MaterialRequestLine) -> MaterialRequest:
    return MaterialRequest.objects.get(pk=line.request_id)


class TestCreate:
    def test_a_handover_from_the_warehouse(
        self,
        org: Org,
        people: People,
        stock_line: MaterialRequestLine,
        hand_over: HandOver,
        published: list[events.Event],
    ) -> None:
        handover = hand_over((stock_line, "4"))
        year = timezone.now().year
        assert handover.number == f"ENT-A-MAD-{year}-00001"
        assert (handover.kind, handover.status) == (
            Handover.Kind.FROM_WAREHOUSE,
            Handover.Status.POSTED,
        )
        assert (handover.branch, handover.source_location, handover.project) == (
            org.a_mad,
            org.warehouses["A Madrid"],
            org.hospital,
        )
        assert (handover.handed_by, handover.received_by, handover.received_by_name) == (
            people.mm_mad,
            people.partner,
            "",
        )
        assert handover.goods_receipt is None
        (row,) = handover.lines.all()
        assert (row.project, row.request_line, row.qty, row.receipt_attribution) == (
            org.hospital,
            stock_line,
            4,
            None,
        )
        assert flows(stock_line).delivered == 4
        assert request_of(stock_line).status == MaterialRequest.Status.APPROVED
        event = AuditEvent.objects.get(action="handover.posted")
        assert (event.actor, event.branch_id) == (people.mm_mad, org.a_mad.pk)
        assert event.data["kind"] == "FROM_WAREHOUSE"
        assert event.data["lines"] == [{"request_line_id": str(stock_line.pk), "qty": "4"}]
        assert [(e.name, e.payload["kind"]) for e in published] == [
            ("handover.posted", "FROM_WAREHOUSE")
        ]

    def test_a_receiver_by_name(self, stock_line: MaterialRequestLine, hand_over: HandOver) -> None:
        handover = hand_over((stock_line, "1"), receiver=Receiver(name="  Juan Pérez "))
        assert (handover.received_by, handover.received_by_name) == (None, "Juan Pérez")

    @pytest.mark.parametrize(
        ("receiver", "code"),
        [
            (lambda p: Receiver(user_id=p.partner.pk, name="Juan"), "receiver_both"),
            (lambda p: Receiver(name="   "), "receiver_required"),
            (lambda p: Receiver(name="x" * 201), "receiver_name_length"),
            (lambda p: Receiver(user_id=p.deactivated.pk), "receiver"),
            (lambda p: Receiver(user_id=p.nobody.pk), "receiver"),
            (lambda p: Receiver(user_id=p.mm_sev.pk), "receiver"),
        ],
    )
    def test_receiver_rules(
        self,
        people: People,
        stock_line: MaterialRequestLine,
        hand_over: HandOver,
        receiver: Any,
        code: str,
    ) -> None:
        with pytest.raises(ValidationError) as exc:
            hand_over((stock_line, "1"), receiver=receiver(people))
        assert codes(exc) == {code}
        assert not Handover.objects.exists()

    def test_an_offboarded_receiver(
        self, people: People, stock_line: MaterialRequestLine, hand_over: HandOver
    ) -> None:
        type(people.tech_mad).objects.filter(pk=people.tech_mad.pk).update(
            offboarded_at=timezone.now()
        )
        with pytest.raises(ValidationError) as exc:
            hand_over((stock_line, "1"), receiver=Receiver(user_id=people.tech_mad.pk))
        assert codes(exc) == {"receiver"}

    def test_lines_of_several_requests(self, make_line: MakeLine, hand_over: HandOver) -> None:
        first, second = make_line("10"), make_line("5", sourcing=STOCK)
        handover = hand_over((first, "2"), (second, "5"))
        assert sorted(handover.lines.values_list("qty", flat=True)) == [2, 5]
        assert request_of(second).status == MaterialRequest.Status.CLOSED

    def test_a_line_of_another_project(
        self, org: Org, make_line: MakeLine, hand_over: HandOver
    ) -> None:
        here, there = make_line("10"), make_line("10", project=org.sev_for_mad)
        with pytest.raises(ValidationError) as exc:
            hand_over((here, "1"), (there, "1"))
        assert codes(exc) == {"unknown"}

    def test_an_unknown_line(self, org: Org, people: People, mm: Any) -> None:
        with pytest.raises(ValidationError) as exc:
            services.create_handover(
                mm,
                org.hospital.pk,
                source_location_id=org.warehouses["A Madrid"].pk,
                lines=[HandoverLineInput(org.hospital.pk, D(1))],
                receiver=Receiver(name="Juan"),
            )
        assert codes(exc) == {"unknown"}

    def test_requests_of_different_branches(
        self, org: Org, people: People, make_line: MakeLine, hand_over: HandOver
    ) -> None:
        mine, other = make_line("10"), make_line("10")
        MaterialRequest.objects.filter(pk=other.request_id).update(procurement_branch=org.a_sev)
        with pytest.raises(ValidationError) as exc:
            hand_over((mine, "1"), (other, "1"), principal=principal_for(people.group_admin))
        assert codes(exc) == {"branches"}

    def test_no_lines(self, org: Org, mm: Any) -> None:
        with pytest.raises(ValidationError) as exc:
            services.create_handover(
                mm,
                org.hospital.pk,
                source_location_id=org.warehouses["A Madrid"].pk,
                lines=[],
                receiver=Receiver(name="Juan"),
            )
        assert codes(exc) == {"empty"}

    def test_one_request_line_twice(
        self, stock_line: MaterialRequestLine, hand_over: HandOver
    ) -> None:
        with pytest.raises(ValueError, match="once per handover"):
            hand_over((stock_line, "1"), (stock_line, "2"))

    @pytest.mark.parametrize("qty", ["0", "-1", "1.5"])
    def test_quantities(
        self, make_line: MakeLine, breaker: Any, hand_over: HandOver, qty: str
    ) -> None:
        line = make_line("10", material=breaker)
        with pytest.raises(ValidationError):
            hand_over((line, qty))
        assert not Handover.objects.exists()

    def test_never_more_than_outstanding(
        self, stock_line: MaterialRequestLine, hand_over: HandOver
    ) -> None:
        hand_over((stock_line, "6"))
        with pytest.raises(ValidationError) as exc:
            hand_over((stock_line, "5"))
        assert codes(exc) == {"over_delivery"}
        assert "4" in str(exc.value)
        hand_over((stock_line, "4"))
        assert flows(stock_line).delivered == 10

    def test_the_outstanding_counts_direct_handovers(
        self, org: Org, make_line: MakeLine, buy: Buy, receive: Receive, hand_over: HandOver
    ) -> None:
        line = make_line("10")
        receive(buy((line, "10")), "7", at=org.worksite(org.hospital))
        with pytest.raises(ValidationError) as exc:
            hand_over((line, "4"))
        assert codes(exc) == {"over_delivery"}
        hand_over((line, "3"))
        assert request_of(line).status == MaterialRequest.Status.CLOSED

    def test_a_full_handover_closes_the_request(
        self, people: People, stock_line: MaterialRequestLine, hand_over: HandOver
    ) -> None:
        hand_over((stock_line, "10"))
        request = request_of(stock_line)
        assert (request.status, request.close_mode) == (
            MaterialRequest.Status.CLOSED,
            MaterialRequest.CloseMode.AUTO,
        )
        assert AuditEvent.objects.get(action="request.auto_closed").actor == people.mm_mad

    @pytest.mark.parametrize("status", ["CLOSED", "SUBMITTED", "CANCELLED"])
    def test_a_request_that_is_not_approved(
        self, make_line: MakeLine, hand_over: HandOver, status: str
    ) -> None:
        line = make_line("10", status=status)
        with pytest.raises(ValidationError) as exc:
            hand_over((line, "1"))
        assert codes(exc) == {"not_open"}

    def test_a_line_closed_short(
        self, people: People, stock_line: MaterialRequestLine, hand_over: HandOver
    ) -> None:
        MaterialRequestLine.objects.filter(pk=stock_line.pk).update(
            closed_short_at=timezone.now(),
            closed_short_by=people.mm_mad,
            closed_short_source="LINE",
            close_reason="Not needed",
        )
        with pytest.raises(ValidationError) as exc:
            hand_over((stock_line, "1"))
        assert codes(exc) == {"not_open"}

    def test_any_sourcing_of_an_open_line(self, make_line: MakeLine, hand_over: HandOver) -> None:
        """Material bought earlier and kept at the warehouse goes out the same way."""
        undecided = make_line("3", sourcing=MaterialRequestLine.Sourcing.UNDECIDED)
        hand_over((undecided, "3"))
        assert flows(undecided).delivered == 3

    @pytest.mark.parametrize("status", [Project.Status.ON_HOLD, Project.Status.CLOSED])
    def test_a_project_that_is_not_active(
        self, org: Org, stock_line: MaterialRequestLine, hand_over: HandOver, status: str
    ) -> None:
        Project.objects.filter(pk=org.hospital.pk).update(status=status)
        with pytest.raises(ValidationError) as exc:
            hand_over((stock_line, "1"))
        assert codes(exc) == {"project_not_active"}

    def test_a_project_of_an_inactive_branch(
        self, org: Org, stock_line: MaterialRequestLine, hand_over: HandOver
    ) -> None:
        Branch.objects.filter(pk=org.b_mad.pk).update(is_active=False)
        with pytest.raises(ValidationError) as exc:
            hand_over((stock_line, "1"))
        assert codes(exc) == {"inactive_branch"}

    def test_requests_of_an_inactive_branch(
        self, org: Org, people: People, stock_line: MaterialRequestLine, hand_over: HandOver
    ) -> None:
        Branch.objects.filter(pk=org.a_mad.pk).update(is_active=False)
        with pytest.raises(ValidationError) as exc:
            hand_over((stock_line, "1"), principal=principal_for(people.group_admin))
        assert codes(exc) == {"inactive_branch"}

    def test_a_warehouse_of_a_managed_branch(
        self, org: Org, stock_line: MaterialRequestLine, hand_over: HandOver
    ) -> None:
        handover = hand_over((stock_line, "1"), source=org.warehouses["B Madrid"])
        assert (handover.branch, handover.source_location) == (
            org.a_mad,
            org.warehouses["B Madrid"],
        )

    def test_a_second_warehouse_of_the_branch(
        self, org: Org, stock_line: MaterialRequestLine, hand_over: HandOver
    ) -> None:
        second = Location.objects.create(
            kind=Location.Kind.WAREHOUSE, branch=org.a_mad, name="wh A Madrid 2"
        )
        assert hand_over((stock_line, "1"), source=second).source_location == second

    def test_sources_that_are_refused(
        self, org: Org, stock_line: MaterialRequestLine, hand_over: HandOver
    ) -> None:
        inactive = Location.objects.create(
            kind=Location.Kind.WAREHOUSE, branch=org.a_mad, name="old", is_active=False
        )
        closed_branch = Branch.objects.create(
            company=org.a, code="X", name="A closed", managed_by=org.a_mad
        )
        of_closed = Location.objects.create(
            kind=Location.Kind.WAREHOUSE, branch=closed_branch, name="wh closed"
        )
        Branch.objects.filter(pk=closed_branch.pk).update(is_active=False)
        for source in (
            org.warehouses["A Sevilla"],  # another branch's
            org.worksite(org.hospital),  # not a warehouse
            inactive,
            of_closed,
        ):
            with pytest.raises(ValidationError) as exc:
                hand_over((stock_line, "1"), source=source)
            assert codes(exc) == {"location"}, source
        assert not Handover.objects.exists()

    def test_numbers_per_branch(
        self,
        org: Org,
        people: People,
        make_line: MakeLine,
        buy: Buy,
        receive: Receive,
        hand_over: HandOver,
    ) -> None:
        year = timezone.now().year
        line = make_line("20")
        receive(buy((line, "5")), "5", at=org.worksite(org.hospital))  # a direct one: 00001
        assert hand_over((line, "1")).number == f"ENT-A-MAD-{year}-00002"
        sevilla = make_line("5", project=org.sev_works, sourcing=STOCK)
        other = hand_over(
            (sevilla, "1"),
            source=org.warehouses["A Sevilla"],
            principal=principal_for(people.mm_sev),
            receiver=Receiver(name="Crew"),
        )
        assert other.number == f"ENT-A-SEV-{year}-00001"

    def test_idempotent(
        self, people: People, stock_line: MaterialRequestLine, hand_over: HandOver
    ) -> None:
        key = issue_key(people.mm_mad.pk, "handover.post")
        first = hand_over((stock_line, "3"), idempotency_key=key)
        again = hand_over((stock_line, "3"), idempotency_key=key)
        assert again == first
        assert Handover.objects.count() == 1
        assert flows(stock_line).delivered == 3
        with pytest.raises(IdempotencyConflict):
            hand_over((stock_line, "4"), idempotency_key=key)
        with pytest.raises(IdempotencyConflict):
            hand_over((stock_line, "3"), idempotency_key=key, receiver=Receiver(name="Juan"))


class TestVoid:
    def test_void(
        self,
        people: People,
        mm: Any,
        stock_line: MaterialRequestLine,
        hand_over: HandOver,
        published: list[events.Event],
    ) -> None:
        handover = hand_over((stock_line, "4"))
        voided = services.void_handover(mm, handover.pk, reason="  Wrong person ")
        assert (voided.status, voided.voided_by, voided.void_reason) == (
            Handover.Status.VOIDED,
            people.mm_mad,
            "Wrong person",
        )
        assert voided.voided_at is not None
        assert flows(stock_line).delivered == 0
        event = AuditEvent.objects.get(action="handover.voided")
        assert event.data["from_status"] == "POSTED"
        assert event.data["reason"] == "Wrong person"
        assert [e.name for e in published] == ["handover.posted", "handover.voided"]

    def test_a_reason_is_required(
        self, mm: Any, stock_line: MaterialRequestLine, hand_over: HandOver
    ) -> None:
        handover = hand_over((stock_line, "4"))
        with pytest.raises(ValidationError) as exc:
            services.void_handover(mm, handover.pk, reason="  ")
        assert codes(exc) == {"required"}

    def test_twice(self, mm: Any, stock_line: MaterialRequestLine, hand_over: HandOver) -> None:
        handover = hand_over((stock_line, "4"))
        services.void_handover(mm, handover.pk, reason="Wrong")
        with pytest.raises(ValidationError) as exc:
            services.void_handover(mm, handover.pk, reason="Again")
        assert codes(exc) == {"transition"}

    def test_reopens_an_auto_closed_request(
        self, mm: Any, stock_line: MaterialRequestLine, hand_over: HandOver
    ) -> None:
        handover = hand_over((stock_line, "10"))
        services.void_handover(mm, handover.pk, reason="Not taken")
        request = request_of(stock_line)
        assert (request.status, request.close_mode, request.closed_at) == (
            MaterialRequest.Status.APPROVED,
            "",
            None,
        )
        assert AuditEvent.objects.filter(action="request.auto_reopened").count() == 1

    def test_a_manual_close_stays_closed(
        self, mm: Any, stock_line: MaterialRequestLine, hand_over: HandOver
    ) -> None:
        handover = hand_over((stock_line, "4"))
        request_services.manual_close(mm, stock_line.request_id, reason="Work finished")
        services.void_handover(mm, handover.pk, reason="Not taken")
        request = request_of(stock_line)
        assert (request.status, request.close_mode) == (
            MaterialRequest.Status.CLOSED,
            MaterialRequest.CloseMode.MANUAL,
        )

    def test_a_direct_receipt_handover_is_refused(
        self, org: Org, mm: Any, make_line: MakeLine, buy: Buy, receive: Receive
    ) -> None:
        line = make_line("10")
        receipt = receive(buy((line, "10")), "6", at=org.worksite(org.hospital))
        direct = Handover.objects.get(goods_receipt=receipt)
        with pytest.raises(ValidationError) as exc:
            services.void_handover(mm, direct.pk, reason="Wrong")
        assert codes(exc) == {"direct_receipt"}
        direct.refresh_from_db()
        assert direct.status == Handover.Status.POSTED
        assert flows(line).delivered == 6
        receiving.void_receipt(mm, receipt.pk, reason="Wrong order")
        direct.refresh_from_db()
        assert direct.status == Handover.Status.VOIDED

    def test_re_entering_after_a_void(
        self, mm: Any, stock_line: MaterialRequestLine, hand_over: HandOver
    ) -> None:
        handover = hand_over((stock_line, "10"))
        services.void_handover(mm, handover.pk, reason="Wrong quantity")
        hand_over((stock_line, "8"))
        assert flows(stock_line).delivered == 8
        assert request_of(stock_line).status == MaterialRequest.Status.APPROVED

    def test_a_request_can_be_cancelled_after_the_void(
        self, mm: Any, stock_line: MaterialRequestLine, hand_over: HandOver
    ) -> None:
        handover = hand_over((stock_line, "4"))
        with pytest.raises(ValidationError):
            request_services.cancel(mm, stock_line.request_id, reason="Not needed")
        services.void_handover(mm, handover.pk, reason="Not taken")
        request_services.cancel(mm, stock_line.request_id, reason="Not needed")
        assert request_of(stock_line).status == MaterialRequest.Status.CANCELLED
