"""Review, sourcing and closing services (plan §4.3, §4.4, §4.5, §4.9).

Called without any view: the services enforce the capabilities, the self-approval rule, the
editing rules by state and the ledger floors themselves. The flows that purchasing and
handovers will contribute (D, R, P, Q, allocated) come from the ``flows_of`` stand-in.
"""

from collections.abc import Callable, Iterator
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from apps.access.exceptions import AccessDenied
from apps.audit.models import AuditEvent
from apps.catalog.models import Material, UnitOfMeasure
from apps.core import events
from apps.material_requests import ledger, services
from apps.material_requests.ledger import Flows
from apps.material_requests.models import MaterialRequest, MaterialRequestLine
from apps.material_requests.services import LineDecision
from apps.material_requests.services import common as common_services
from apps.material_requests.services import review as review_services
from apps.organizations.models import Branch
from tests.org import Org, People
from tests.support import error_codes, principal_for
from tests.unit.material_requests.conftest import MakeRequest

pytestmark = pytest.mark.django_db

Status = MaterialRequest.Status
Decision = MaterialRequestLine.Decision
Sourcing = MaterialRequestLine.Sourcing
D = Decimal


def actions() -> list[str]:
    return list(AuditEvent.objects.order_by("occurred_at", "id").values_list("action", flat=True))


def fresh(obj: Any) -> Any:
    return type(obj).objects.get(pk=obj.pk)


def lines_of(request: MaterialRequest) -> list[MaterialRequestLine]:
    return list(MaterialRequestLine.objects.filter(request=request).order_by("line_no"))


@pytest.fixture
def published() -> Iterator[list[events.Event]]:
    seen: list[events.Event] = []
    names = [
        "request_line.decided",
        "request.approved",
        "request.rejected",
        "request.returned",
        "request_line.reduced",
        "request_line.sourcing_set",
        "request_line.resolved",
        "request.rescheduled",
        "request_line.closed_short",
        "request_line.reopened",
        "request.closed",
        "request.reopened",
        "request.auto_closed",
        "request.auto_reopened",
    ]
    undo = [events.subscribe(name, seen.append) for name in names]
    yield seen
    for unsubscribe in undo:
        unsubscribe()


@pytest.fixture
def approved(make_request: MakeRequest) -> MaterialRequest:
    """An approved hospital request with two lines approved at 5 of 10."""
    return make_request(Status.APPROVED, lines=2, decided=True)


def non_catalog(request: MaterialRequest, *, unit: str = "M", line_no: int = 9) -> Any:
    """A free-text line, approved at 4 of 4."""
    return MaterialRequestLine.objects.create(
        request=request,
        project_id=request.project_id,
        line_no=line_no,
        free_text="Bandeja perforada 300",
        unit=UnitOfMeasure.objects.get(code=unit),
        requested_qty=D(4),
        decision=Decision.APPROVED,
        approved_qty=D(4),
        decided_at=request.created_at,
    )


def mm(people: People) -> Any:
    return principal_for(people.mm_mad)


# --- decisions ------------------------------------------------------------------------


class TestDecide:
    def test_partial_approval_keeps_both_quantities(
        self, make_request: MakeRequest, people: People, published: list[events.Event]
    ) -> None:
        request = make_request(Status.SUBMITTED)
        line = request.lines.get()
        MaterialRequestLine.objects.filter(pk=line.pk).update(requested_qty=D(20))
        decided = services.decide_line(
            mm(people), line.pk, decision=Decision.APPROVED, approved_qty=D(15)
        )
        assert (decided.requested_qty, decided.approved_qty, decided.decision) == (
            D(20),
            D(15),
            Decision.APPROVED,
        )
        assert (decided.decided_by_id, decided.decided_at) == (people.mm_mad.pk, decided.decided_at)
        after = fresh(request)
        assert after.status == Status.APPROVED and after.decided_at is not None
        assert actions() == ["request_line.decided", "request.approved"]
        assert [event.name for event in published] == ["request_line.decided", "request.approved"]
        assert AuditEvent.objects.get(action="request_line.decided").data["changes"][
            "approved_qty"
        ] == {"from": None, "to": "15"}

    def test_the_requested_quantity_by_default(
        self, make_request: MakeRequest, people: People
    ) -> None:
        line = make_request(Status.SUBMITTED).lines.get()
        decided = services.decide_line(mm(people), line.pk, decision=Decision.APPROVED)
        assert decided.approved_qty == D(10)

    def test_the_request_stays_submitted_until_every_line_is_decided(
        self, make_request: MakeRequest, people: People
    ) -> None:
        request = make_request(Status.SUBMITTED, lines=2)
        first, second = lines_of(request)
        services.decide_line(mm(people), first.pk, decision=Decision.APPROVED)
        assert fresh(request).status == Status.SUBMITTED
        # Own decisions can change until all lines are decided.
        services.decide_line(mm(people), first.pk, decision=Decision.REJECTED, note="Duplicado")
        services.decide_line(mm(people), second.pk, decision=Decision.APPROVED, approved_qty=D(2))
        assert fresh(request).status == Status.APPROVED
        assert fresh(first).decision == Decision.REJECTED

    def test_all_rejected_rejects_the_request(
        self, make_request: MakeRequest, people: People, published: list[events.Event]
    ) -> None:
        request = make_request(Status.SUBMITTED, lines=2)
        services.decide_lines(
            mm(people),
            request.pk,
            [LineDecision(line.pk, Decision.REJECTED, note=" No ") for line in lines_of(request)],
        )
        assert fresh(request).status == Status.REJECTED
        assert {line.decision_note for line in lines_of(request)} == {"No"}
        assert [line.approved_qty for line in lines_of(request)] == [None, None]
        assert actions()[-1] == "request.rejected"

    def test_a_rejection_needs_a_note(self, make_request: MakeRequest, people: People) -> None:
        line = make_request(Status.SUBMITTED).lines.get()
        with pytest.raises(ValidationError) as exc:
            services.decide_line(mm(people), line.pk, decision=Decision.REJECTED, note="  ")
        assert error_codes(exc) == {"required"}

    @pytest.mark.parametrize(
        ("quantity", "code"),
        [
            (D("10.01"), "above_requested"),
            (D(0), "positive"),
            (D(-1), "positive"),
            (D("1.001"), "precision"),  # metres take two decimals
            (D("NaN"), "invalid"),
        ],
    )
    def test_approved_quantity_rules(
        self, make_request: MakeRequest, people: People, quantity: Decimal, code: str
    ) -> None:
        line = make_request(Status.SUBMITTED).lines.get()
        with pytest.raises(ValidationError) as exc:
            services.decide_line(
                mm(people), line.pk, decision=Decision.APPROVED, approved_qty=quantity
            )
        assert error_codes(exc) == {code}
        assert fresh(line).decision == Decision.PENDING

    def test_an_unknown_decision(self, make_request: MakeRequest, people: People) -> None:
        line = make_request(Status.SUBMITTED).lines.get()
        with pytest.raises(ValidationError) as exc:
            services.decide_line(mm(people), line.pk, decision=Decision.PENDING)
        assert error_codes(exc) == {"invalid"}

    def test_another_reviewers_decision_stays_theirs(
        self, make_request: MakeRequest, people: People
    ) -> None:
        request = make_request(Status.SUBMITTED, lines=2)
        first, _second = lines_of(request)
        services.decide_line(principal_for(people.site_manager), first.pk, decision="APPROVED")
        with pytest.raises(ValidationError) as exc:
            services.decide_line(mm(people), first.pk, decision="REJECTED", note="No")
        assert error_codes(exc) == {"decided_by_other"}

    def test_repeating_a_decision_changes_nothing(
        self, make_request: MakeRequest, people: People
    ) -> None:
        request = make_request(Status.SUBMITTED, lines=2)
        first = lines_of(request)[0]
        for _ in range(2):
            services.decide_line(mm(people), first.pk, decision="APPROVED", approved_qty=D(3))
        assert actions() == ["request_line.decided"]

    def test_batch_rules(self, make_request: MakeRequest, people: People) -> None:
        request, other = make_request(Status.SUBMITTED), make_request(Status.SUBMITTED)
        line = request.lines.get()
        with pytest.raises(ValidationError) as exc:
            services.decide_lines(mm(people), request.pk, [])
        assert error_codes(exc) == {"empty"}
        with pytest.raises(ValueError, match="once"):
            services.decide_lines(mm(people), request.pk, [LineDecision(line.pk, "APPROVED")] * 2)
        with pytest.raises(ValidationError) as exc:
            services.decide_lines(
                mm(people), request.pk, [LineDecision(other.lines.get().pk, "APPROVED")]
            )
        assert error_codes(exc) == {"unknown_line"}

    def test_a_submit_never_approves_by_itself(
        self, make_request: MakeRequest, org: Org, people: People
    ) -> None:
        """Every request is reviewed (Q4), even where self-approval is allowed."""
        Branch.objects.filter(pk=org.a_mad.pk).update(allow_self_approval=True)
        draft = make_request(user=people.mm_mad)
        submitted = services.submit(mm(people), draft.pk)
        assert submitted.status == Status.SUBMITTED
        assert submitted.lines.get().decision == Decision.PENDING

    def test_partner_requests_are_reviewed(self, make_request: MakeRequest, people: People) -> None:
        request = make_request(Status.SUBMITTED, user=people.partner)
        services.approve_all(principal_for(people.site_manager), request.pk)
        assert fresh(request).status == Status.APPROVED


class TestSelfApproval:
    @pytest.mark.parametrize("person", ["mm_mad", "site_manager", "group_admin"])
    def test_nobody_decides_their_own_request(
        self, make_request: MakeRequest, people: People, person: str
    ) -> None:
        user = getattr(people, person)
        request = make_request(Status.SUBMITTED, user=user)
        line = request.lines.get()
        with pytest.raises(AccessDenied):
            services.decide_line(principal_for(user), line.pk, decision="APPROVED")
        with pytest.raises(AccessDenied):
            services.decide_line(principal_for(user), line.pk, decision="REJECTED", note="x")
        with pytest.raises(AccessDenied):
            services.approve_all(principal_for(user), request.pk)
        assert fresh(line).decision == Decision.PENDING

    def test_unless_the_procurement_branch_allows_it(
        self, make_request: MakeRequest, org: Org, people: People
    ) -> None:
        request = make_request(Status.SUBMITTED, user=people.mm_mad)
        Branch.objects.filter(pk=org.b_mad.pk).update(allow_self_approval=True)  # project's
        with pytest.raises(AccessDenied):
            services.approve_all(mm(people), request.pk)
        Branch.objects.filter(pk=org.a_mad.pk).update(allow_self_approval=True)  # procurement
        assert services.approve_all(mm(people), request.pk).status == Status.APPROVED


class TestApproveAll:
    def test_undecided_lines_at_their_requested_quantity(
        self, make_request: MakeRequest, people: People
    ) -> None:
        request = make_request(Status.SUBMITTED, lines=3)
        first, second, third = lines_of(request)
        services.decide_line(mm(people), first.pk, decision="REJECTED", note="No")
        services.decide_line(mm(people), second.pk, decision="APPROVED", approved_qty=D(1))
        after = services.approve_all(mm(people), request.pk, note=" ok ")
        assert after.status == Status.APPROVED
        assert [(line.decision, line.approved_qty) for line in lines_of(request)] == [
            (Decision.REJECTED, None),
            (Decision.APPROVED, D(1)),
            (Decision.APPROVED, D(10)),
        ]
        assert fresh(third).decision_note == "ok"


# --- return to draft ------------------------------------------------------------------


class TestReturnToDraft:
    def test_every_decision_is_cleared(
        self, make_request: MakeRequest, people: People, published: list[events.Event]
    ) -> None:
        request = make_request(Status.SUBMITTED, lines=2)
        first, _second = lines_of(request)
        services.decide_line(mm(people), first.pk, decision="REJECTED", note="Falta medida")
        draft = services.return_to_draft(mm(people), request.pk, note=" Indica la medida ")
        assert (draft.status, draft.number, draft.decided_at) == (
            Status.DRAFT,
            request.number,
            None,
        )
        for line in lines_of(request):
            assert (
                line.decision,
                line.approved_qty,
                line.decided_by_id,
                line.decided_at,
                line.decision_note,
                line.sourcing,
            ) == (Decision.PENDING, None, None, None, "", Sourcing.UNDECIDED)
        event = AuditEvent.objects.get(action="request.returned")
        assert (event.data["from_status"], event.data["note"]) == ("SUBMITTED", "Indica la medida")
        assert published[-1].name == "request.returned"

    def test_an_approved_request_loses_its_closures_and_sourcing(
        self, approved: MaterialRequest, people: People
    ) -> None:
        first, second = lines_of(approved)
        services.set_sourcing(mm(people), first.pk, sourcing="PURCHASE")
        services.close_line_short(mm(people), second.pk, reason="Ya no")
        draft = services.return_to_draft(principal_for(people.site_manager), approved.pk, note="x")
        assert draft.status == Status.DRAFT
        assert {
            (line.decision, line.sourcing, line.closed_short_at, line.closed_short_source)
            for line in lines_of(approved)
        } == {(Decision.PENDING, Sourcing.UNDECIDED, None, "")}
        # The requester edits and submits it again; it needs a new review.
        resubmitted = services.submit(principal_for(people.tech_mad), approved.pk)
        assert (resubmitted.status, resubmitted.number) == (Status.SUBMITTED, approved.number)

    def test_a_note_is_required(self, approved: MaterialRequest, people: People) -> None:
        with pytest.raises(ValidationError) as exc:
            services.return_to_draft(mm(people), approved.pk, note=" ")
        assert error_codes(exc) == {"required"}

    @pytest.mark.parametrize(
        "flow",
        [Flows(allocated=D(1)), Flows(delivered=D(1)), Flows(allocated=D(2), delivered=D(1))],
    )
    def test_not_once_anything_is_allocated_or_delivered(
        self, approved: MaterialRequest, people: People, flows_of: dict[UUID, Flows], flow: Flows
    ) -> None:
        flows_of[lines_of(approved)[1].pk] = flow
        with pytest.raises(ValidationError) as exc:
            services.return_to_draft(mm(people), approved.pk, note="x")
        assert error_codes(exc) == {"in_progress"}
        with pytest.raises(ValidationError) as exc:
            services.cancel(mm(people), approved.pk)
        assert error_codes(exc) == {"in_progress"}
        assert fresh(approved).status == Status.APPROVED

    def test_the_requester_cannot(self, make_request: MakeRequest, people: People) -> None:
        with pytest.raises(AccessDenied):
            services.return_to_draft(
                principal_for(people.tech_mad), make_request(Status.SUBMITTED).pk, note="x"
            )


# --- approved-quantity reduction ------------------------------------------------------


class TestReduce:
    def test_a_live_line(
        self, approved: MaterialRequest, people: People, published: list[events.Event]
    ) -> None:
        line = lines_of(approved)[0]
        reduced = services.reduce_approved_qty(
            mm(people), line.pk, approved_qty=D(3), reason=" Sobra "
        )
        assert (reduced.requested_qty, reduced.approved_qty) == (D(10), D(3))
        event = AuditEvent.objects.get(action="request_line.reduced")
        assert event.data["changes"]["approved_qty"] == {"from": "5.000", "to": "3"}
        assert event.data["reason"] == "Sobra"
        assert [event.name for event in published] == ["request_line.reduced"]
        assert fresh(approved).status == Status.APPROVED

    def test_never_up_and_unchanged_is_a_no_op(
        self, approved: MaterialRequest, people: People
    ) -> None:
        line = lines_of(approved)[0]
        with pytest.raises(ValidationError) as exc:
            services.reduce_approved_qty(mm(people), line.pk, approved_qty=D(6))
        assert error_codes(exc) == {"not_lower"}
        services.reduce_approved_qty(mm(people), line.pk, approved_qty=D(5))
        assert actions() == []

    @pytest.mark.parametrize(("quantity", "code"), [(D(-1), "positive"), (D("1.001"), "precision")])
    def test_quantity_rules(
        self, approved: MaterialRequest, people: People, quantity: Decimal, code: str
    ) -> None:
        with pytest.raises(ValidationError) as exc:
            services.reduce_approved_qty(
                mm(people), lines_of(approved)[0].pk, approved_qty=quantity
            )
        assert error_codes(exc) == {code}

    def test_never_below_max_d_r_plus_p_plus_q(
        self, approved: MaterialRequest, people: People, flows_of: dict[UUID, Flows]
    ) -> None:
        line = lines_of(approved)[0]
        flows_of[line.pk] = Flows(
            delivered=D(1), received=D(2), pending=D("0.5"), reserved=D("0.5")
        )
        with pytest.raises(ValidationError) as exc:
            services.reduce_approved_qty(mm(people), line.pk, approved_qty=D("2.99"))
        assert error_codes(exc) == {"below_commitment"}
        assert (
            services.reduce_approved_qty(mm(people), line.pk, approved_qty=D(3)).approved_qty == 3
        )

    def test_a_closed_short_line_is_refused(
        self, approved: MaterialRequest, people: People
    ) -> None:
        line = lines_of(approved)[0]
        services.close_line_short(mm(people), line.pk, reason="x")
        with pytest.raises(ValidationError) as exc:
            services.reduce_approved_qty(mm(people), line.pk, approved_qty=D(1))
        assert error_codes(exc) == {"closed_short"}
        assert fresh(line).approved_qty == D(5)

    def test_a_rejected_line_is_refused(self, make_request: MakeRequest, people: People) -> None:
        request = make_request(Status.SUBMITTED, lines=2)
        first, second = lines_of(request)
        services.decide_lines(
            mm(people),
            request.pk,
            [LineDecision(first.pk, "APPROVED"), LineDecision(second.pk, "REJECTED", note="No")],
        )
        with pytest.raises(ValidationError) as exc:
            services.reduce_approved_qty(mm(people), second.pk, approved_qty=D(0))
        assert error_codes(exc) == {"not_live"}

    def test_reducing_the_last_live_line_to_zero_closes_the_request(
        self, make_request: MakeRequest, people: People, published: list[events.Event]
    ) -> None:
        request = make_request(Status.APPROVED, decided=True)
        services.reduce_approved_qty(mm(people), request.lines.get().pk, approved_qty=D(0))
        after = fresh(request)
        assert (after.status, after.close_mode) == (Status.CLOSED, "AUTO")
        assert after.closed_at is not None
        assert actions() == ["request_line.reduced", "request.auto_closed"]
        assert published[-1].name == "request.auto_closed"

    def test_reducing_to_what_was_delivered_closes_the_request(
        self, approved: MaterialRequest, people: People, flows_of: dict[UUID, Flows]
    ) -> None:
        first, second = lines_of(approved)
        flows_of[first.pk] = Flows(delivered=D(5))
        flows_of[second.pk] = Flows(delivered=D(2))
        services.reduce_approved_qty(mm(people), second.pk, approved_qty=D(2))
        assert fresh(approved).status == Status.CLOSED

    def test_the_requester_cannot(self, approved: MaterialRequest, people: People) -> None:
        with pytest.raises(AccessDenied):
            services.reduce_approved_qty(
                principal_for(people.tech_mad), lines_of(approved)[0].pk, approved_qty=D(1)
            )


def test_i1_and_i13_hold_through_any_sequence(
    make_request: MakeRequest, people: People, flows_of: dict[UUID, Flows]
) -> None:
    """Reduce, close short and reopen in any order, with any delivered and received
    quantities: A ≤ requested (I1), D ≤ A and R ≤ A (I5, I13) always hold."""
    request = make_request(Status.APPROVED, decided=True)
    line_id = request.lines.get().pk
    principal = mm(people)

    @settings(
        max_examples=60,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(
        delivered=st.integers(0, 5),
        received=st.integers(0, 5),
        steps=st.lists(
            st.one_of(
                st.tuples(st.just("reduce"), st.integers(0, 6)),
                st.tuples(st.just("close"), st.just(0)),
                st.tuples(st.just("reopen"), st.just(0)),
            ),
            max_size=6,
        ),
    )
    def run(delivered: int, received: int, steps: list[tuple[str, int]]) -> None:
        with transaction.atomic():
            flows_of[line_id] = Flows(delivered=D(delivered), received=D(received))
            for operation, quantity in steps:
                try:
                    if operation == "reduce":
                        services.reduce_approved_qty(principal, line_id, approved_qty=D(quantity))
                    elif operation == "close":
                        services.close_line_short(principal, line_id, reason="x")
                    elif fresh(request).status == Status.CLOSED:
                        services.reopen(principal, request.pk, reason="x")
                    else:
                        services.reopen_line(principal, line_id, reason="x")
                except ValidationError:
                    pass
                line = MaterialRequestLine.objects.get(pk=line_id)
                assert line.approved_qty is not None
                assert line.approved_qty <= line.requested_qty
                assert max(delivered, received) <= line.approved_qty
            transaction.set_rollback(True)

    run()


def test_i1_is_held_by_the_database(approved: MaterialRequest) -> None:
    line = lines_of(approved)[0]
    with pytest.raises(IntegrityError), transaction.atomic():
        MaterialRequestLine.objects.filter(pk=line.pk).update(approved_qty=D("10.001"))


# --- sourcing -------------------------------------------------------------------------


class TestSourcing:
    def test_stock_and_purchase(
        self, approved: MaterialRequest, people: People, published: list[events.Event]
    ) -> None:
        line = lines_of(approved)[0]
        assert (
            services.set_sourcing(mm(people), line.pk, sourcing="PURCHASE").sourcing == "PURCHASE"
        )
        assert services.set_sourcing(mm(people), line.pk, sourcing="STOCK").sourcing == "STOCK"
        assert (
            services.set_sourcing(mm(people), line.pk, sourcing="PURCHASE").sourcing == "PURCHASE"
        )
        services.set_sourcing(mm(people), line.pk, sourcing="PURCHASE")  # unchanged
        assert actions() == ["request_line.sourcing_set"] * 3
        assert [event.name for event in published] == ["request_line.sourcing_set"] * 3

    @pytest.mark.parametrize("flow", [Flows(pending=D(1)), Flows(reserved=D(1))])
    def test_purchase_to_stock_only_with_nothing_on_order(
        self, approved: MaterialRequest, people: People, flows_of: dict[UUID, Flows], flow: Flows
    ) -> None:
        line = lines_of(approved)[0]
        services.set_sourcing(mm(people), line.pk, sourcing="PURCHASE")
        flows_of[line.pk] = flow
        with pytest.raises(ValidationError) as exc:
            services.set_sourcing(mm(people), line.pk, sourcing="STOCK")
        assert error_codes(exc) == {"purchase_in_progress"}
        flows_of[line.pk] = Flows(received=D(5))  # received, nothing pending: allowed
        assert services.set_sourcing(mm(people), line.pk, sourcing="STOCK").sourcing == "STOCK"

    def test_a_non_catalog_line_is_resolved_before_purchase(
        self, approved: MaterialRequest, people: People
    ) -> None:
        line = non_catalog(approved)
        with pytest.raises(ValidationError) as exc:
            services.set_sourcing(mm(people), line.pk, sourcing="PURCHASE")
        assert error_codes(exc) == {"needs_material"}
        assert services.set_sourcing(mm(people), line.pk, sourcing="STOCK").sourcing == "STOCK"

    @pytest.mark.parametrize("value", ["UNDECIDED", "BUY"])
    def test_an_unknown_value(self, approved: MaterialRequest, people: People, value: str) -> None:
        with pytest.raises(ValidationError) as exc:
            services.set_sourcing(mm(people), lines_of(approved)[0].pk, sourcing=value)
        assert error_codes(exc) == {"invalid"}

    def test_only_open_lines(self, approved: MaterialRequest, people: People) -> None:
        line = lines_of(approved)[0]
        services.close_line_short(mm(people), line.pk, reason="x")
        with pytest.raises(ValidationError) as exc:
            services.set_sourcing(mm(people), line.pk, sourcing="STOCK")
        assert error_codes(exc) == {"closed_short"}


class TestResolve:
    def test_link_a_material_in_the_lines_unit(
        self, approved: MaterialRequest, people: People, cable: Material
    ) -> None:
        line = non_catalog(approved)
        resolved = services.resolve_non_catalog_line(mm(people), line.pk, material_id=cable.pk)
        assert (resolved.material_id, resolved.free_text) == (cable.pk, "Bandeja perforada 300")
        assert AuditEvent.objects.get(action="request_line.resolved").data["material_id"] == str(
            cable.pk
        )
        assert (
            services.set_sourcing(mm(people), line.pk, sourcing="PURCHASE").sourcing == "PURCHASE"
        )

    def test_rules(
        self, approved: MaterialRequest, people: People, cable: Material, breaker: Material
    ) -> None:
        line = non_catalog(approved)
        with pytest.raises(ValidationError) as exc:
            services.resolve_non_catalog_line(mm(people), line.pk, material_id=breaker.pk)
        assert error_codes(exc) == {"unit_mismatch"}
        Material.objects.filter(pk=cable.pk).update(is_active=False)
        with pytest.raises(ValidationError) as exc:
            services.resolve_non_catalog_line(mm(people), line.pk, material_id=cable.pk)
        assert error_codes(exc) == {"inactive"}
        with pytest.raises(ValidationError) as exc:
            services.resolve_non_catalog_line(
                mm(people), lines_of(approved)[0].pk, material_id=breaker.pk
            )
        assert error_codes(exc) == {"catalog_line"}

    def test_a_unit_change_racing_the_link_is_refused(
        self, approved: MaterialRequest, people: People, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The composite key (C9) refuses the link if the material's unit moved after the
        check; the line is refused, never converted."""
        line = non_catalog(approved, unit="M")
        tray = Material.objects.create(reference="BAN-300", name="Bandeja 300", unit=line.unit)
        original = common_services._save_line

        def unit_changes_first(target: MaterialRequestLine) -> None:
            Material.objects.filter(pk=tray.pk).update(unit=UnitOfMeasure.objects.get(code="KG"))
            original(target)

        monkeypatch.setattr(review_services, "_save_line", unit_changes_first)
        with pytest.raises(ValidationError) as exc:
            services.resolve_non_catalog_line(mm(people), line.pk, material_id=tray.pk)
        assert set(exc.value.message_dict) == {"material"}
        assert error_codes(exc) == {"constraint"}
        assert fresh(line).material_id is None


# --- priority and needed-by -----------------------------------------------------------


class TestSchedule:
    def test_priority_and_needed_by(
        self, approved: MaterialRequest, people: People, published: list[events.Event]
    ) -> None:
        services.change_priority(mm(people), approved.pk, priority="URGENT")
        after = services.change_needed_by(mm(people), approved.pk, needed_by=date(2026, 12, 1))
        assert (after.priority, after.needed_by) == ("URGENT", date(2026, 12, 1))
        services.change_needed_by(mm(people), approved.pk, needed_by=None)
        assert fresh(approved).needed_by is None
        changes = [
            event.data["changes"]
            for event in AuditEvent.objects.filter(action="request.rescheduled").order_by(
                "occurred_at", "id"
            )
        ]
        assert changes == [
            {"priority": {"from": "NORMAL", "to": "URGENT"}},
            {"needed_by": {"from": None, "to": "2026-12-01"}},
            {"needed_by": {"from": "2026-12-01", "to": None}},
        ]
        assert [event.name for event in published] == ["request.rescheduled"] * 3

    def test_unchanged_and_unknown(self, approved: MaterialRequest, people: People) -> None:
        services.change_priority(mm(people), approved.pk, priority="NORMAL")
        services.change_schedule(mm(people), approved.pk)
        assert actions() == []
        with pytest.raises(ValidationError):
            services.change_priority(mm(people), approved.pk, priority="NOW")

    def test_only_approved_requests(self, make_request: MakeRequest, people: People) -> None:
        with pytest.raises(ValidationError) as exc:
            services.change_priority(mm(people), make_request(Status.SUBMITTED).pk, priority="HIGH")
        assert error_codes(exc) == {"transition"}


# --- closing and reopening ------------------------------------------------------------


class TestCloseLineShort:
    def test_close_one_line(
        self, approved: MaterialRequest, people: People, published: list[events.Event]
    ) -> None:
        first, second = lines_of(approved)
        closed = services.close_line_short(mm(people), first.pk, reason=" Descatalogado ")
        assert (closed.closed_short_source, closed.closed_short_by_id, closed.close_reason) == (
            "LINE",
            people.mm_mad.pk,
            "Descatalogado",
        )
        assert closed.closed_short_at is not None
        assert fresh(approved).status == Status.APPROVED  # the second line is still live
        assert ledger.is_live(fresh(second))
        assert [event.name for event in published] == ["request_line.closed_short"]

    def test_closing_the_last_live_line_closes_the_request(
        self, approved: MaterialRequest, people: People
    ) -> None:
        for line in lines_of(approved):
            services.close_line_short(mm(people), line.pk, reason="x")
        after = fresh(approved)
        assert (after.status, after.close_mode, after.close_reason) == (Status.CLOSED, "AUTO", "")
        assert actions()[-1] == "request.auto_closed"
        # An automatic close sets no line flags of its own.
        assert {line.closed_short_source for line in lines_of(approved)} == {"LINE"}

    def test_rules(self, approved: MaterialRequest, people: People) -> None:
        line = lines_of(approved)[0]
        with pytest.raises(ValidationError) as exc:
            services.close_line_short(mm(people), line.pk, reason="")
        assert error_codes(exc) == {"required"}
        services.close_line_short(mm(people), line.pk, reason="x")
        with pytest.raises(ValidationError) as exc:
            services.close_line_short(mm(people), line.pk, reason="x")
        assert error_codes(exc) == {"closed_short"}


class TestReopenLine:
    def test_a_line_closed_on_its_own(
        self, approved: MaterialRequest, people: People, published: list[events.Event]
    ) -> None:
        line = lines_of(approved)[0]
        services.close_line_short(mm(people), line.pk, reason="Descatalogado")
        reopened = services.reopen_line(mm(people), line.pk, reason=" Vuelve ")
        assert (reopened.closed_short_at, reopened.closed_short_source, reopened.close_reason) == (
            None,
            "",
            "",
        )
        event = AuditEvent.objects.get(action="request_line.reopened")
        assert (event.data["reason"], event.data["closed"]["close_reason"]) == (
            "Vuelve",
            "Descatalogado",
        )
        assert published[-1].name == "request_line.reopened"

    def test_rules(self, approved: MaterialRequest, people: People) -> None:
        line = lines_of(approved)[0]
        with pytest.raises(ValidationError) as exc:
            services.reopen_line(mm(people), line.pk, reason="x")
        assert error_codes(exc) == {"not_closed"}
        services.close_line_short(mm(people), line.pk, reason="x")
        with pytest.raises(ValidationError) as exc:
            services.reopen_line(mm(people), line.pk, reason=" ")
        assert error_codes(exc) == {"required"}


class TestFullyDeliveredLines:
    """A line with D = A has nothing short: closing never marks it closed short."""

    def test_close_line_short_refuses_a_delivered_line(
        self, approved: MaterialRequest, people: People, flows_of: dict[UUID, Flows]
    ) -> None:
        line = lines_of(approved)[0]
        flows_of[line.pk] = Flows(delivered=line.approved_qty)
        with pytest.raises(ValidationError) as caught:
            services.close_line_short(mm(people), line.pk, reason="x")
        assert error_codes(caught.value) == {"fully_delivered"}
        assert fresh(line).closed_short_at is None

    def test_a_manual_close_leaves_a_delivered_line_alone(
        self, make_request: MakeRequest, people: People, flows_of: dict[UUID, Flows]
    ) -> None:
        request = make_request(Status.APPROVED, lines=2, decided=True)
        delivered, short = lines_of(request)
        flows_of[delivered.pk] = Flows(delivered=delivered.approved_qty)
        services.manual_close(mm(people), request.pk, reason="Works finished")
        assert fresh(delivered).closed_short_at is None
        assert fresh(short).closed_short_source == "REQUEST"
        assert AuditEvent.objects.get(action="request.closed").data["lines"] == [2]


class TestManualCloseAndReopen:
    def test_a_reopen_restores_only_the_lines_the_close_closed(
        self, make_request: MakeRequest, people: People, published: list[events.Event]
    ) -> None:
        request = make_request(Status.APPROVED, lines=3, decided=True)
        own, _live, rejected = lines_of(request)
        MaterialRequestLine.objects.filter(pk=rejected.pk).update(
            decision=Decision.REJECTED, approved_qty=None
        )
        services.close_line_short(mm(people), own.pk, reason="Por línea")
        closed = services.manual_close(mm(people), request.pk, reason=" Obra terminada ")
        assert (closed.status, closed.close_mode, closed.close_reason) == (
            Status.CLOSED,
            "MANUAL",
            "Obra terminada",
        )
        assert [(line.closed_short_source, line.close_reason) for line in lines_of(request)] == [
            ("LINE", "Por línea"),
            ("REQUEST", "Obra terminada"),
            ("", ""),
        ]
        assert AuditEvent.objects.get(action="request.closed").data["lines"] == [2]

        reopened = services.reopen(mm(people), request.pk, reason="Falta material")
        assert (reopened.status, reopened.close_mode, reopened.closed_at) == (
            Status.APPROVED,
            "",
            None,
        )
        assert [line.closed_short_source for line in lines_of(request)] == ["LINE", "", ""]
        event = AuditEvent.objects.get(action="request.reopened")
        assert (event.data["lines"], event.data["close_mode"]) == ([2], "MANUAL")
        assert [event.name for event in published][-2:] == ["request.closed", "request.reopened"]
        services.reopen_line(mm(people), own.pk, reason="También")
        assert all(ledger.is_live(line) for line in lines_of(request)[:2])

    def test_reopening_an_automatic_close(self, approved: MaterialRequest, people: People) -> None:
        for line in lines_of(approved):
            services.close_line_short(mm(people), line.pk, reason="x")
        reopened = services.reopen(mm(people), approved.pk, reason="Error")
        assert reopened.status == Status.APPROVED  # and stays open: no automatic re-close
        assert {line.closed_short_source for line in lines_of(approved)} == {"LINE"}

    def test_reasons_and_states(self, approved: MaterialRequest, people: People) -> None:
        with pytest.raises(ValidationError) as exc:
            services.manual_close(mm(people), approved.pk, reason="")
        assert error_codes(exc) == {"required"}
        with pytest.raises(ValidationError) as exc:
            services.reopen(mm(people), approved.pk, reason="x")
        assert error_codes(exc) == {"transition"}
        services.manual_close(mm(people), approved.pk, reason="x")
        with pytest.raises(ValidationError) as exc:
            services.manual_close(mm(people), approved.pk, reason="x")
        assert error_codes(exc) == {"transition"}
        with pytest.raises(ValidationError) as exc:
            services.reopen(mm(people), approved.pk, reason="")
        assert error_codes(exc) == {"required"}

    def test_closed_short_lines_never_appear_as_purchasable(
        self, approved: MaterialRequest, people: People
    ) -> None:
        def purchasable() -> list[int]:
            lines = MaterialRequestLine.objects.select_related("request").order_by("line_no")
            return [entry.line.line_no for entry in ledger.purchasable(lines)]

        first, second = lines_of(approved)
        for line in (first, second):
            services.set_sourcing(mm(people), line.pk, sourcing="PURCHASE")
        assert purchasable() == [1, 2]
        services.close_line_short(mm(people), first.pk, reason="x")
        assert purchasable() == [2]
        services.manual_close(mm(people), approved.pk, reason="x")
        assert purchasable() == []
        services.reopen(mm(people), approved.pk, reason="x")
        assert purchasable() == [2]


class TestAutomaticCloseAndReopen:
    """``_settle_closure`` is what handovers (M6.2) call after D changes."""

    def settle(self, request: MaterialRequest, people: People) -> MaterialRequest:
        with transaction.atomic():
            locked = MaterialRequest.objects.get(pk=request.pk)
            services._settle_closure(mm(people), locked, list(locked.lines.all()))
        return MaterialRequest.objects.get(pk=request.pk)

    def test_closes_when_every_live_line_is_delivered(
        self, approved: MaterialRequest, people: People, flows_of: dict[UUID, Flows]
    ) -> None:
        first, second = lines_of(approved)
        flows_of[first.pk] = Flows(delivered=D(5))
        assert self.settle(approved, people).status == Status.APPROVED
        flows_of[second.pk] = Flows(delivered=D(5))
        assert self.settle(approved, people).close_mode == "AUTO"

    def test_reopens_an_automatic_close_when_a_line_is_short_again(
        self, make_request: MakeRequest, people: People, flows_of: dict[UUID, Flows]
    ) -> None:
        request = make_request(Status.CLOSED, decided=True)  # AUTO, nothing delivered
        flows_of[request.lines.get().pk] = Flows(delivered=D(5))
        assert self.settle(request, people).status == Status.CLOSED
        flows_of.clear()  # a handover void
        after = self.settle(request, people)
        assert (after.status, after.close_mode, after.closed_at) == (Status.APPROVED, "", None)
        assert actions() == ["request.auto_reopened"]

    def test_a_manual_close_never_reopens_by_itself(
        self, approved: MaterialRequest, people: People
    ) -> None:
        services.manual_close(mm(people), approved.pk, reason="x")
        assert self.settle(approved, people).status == Status.CLOSED


# --- the state rules, operation by operation ------------------------------------------

# Which state each review or sourcing operation takes (plan §4.4), for a material manager
# of the procurement branch with session MFA, on someone else's request.
ALLOWED_IN = {
    "decide_line": {Status.SUBMITTED},
    "approve_all": {Status.SUBMITTED},
    "return_to_draft": {Status.SUBMITTED, Status.APPROVED},
    "reduce_approved_qty": {Status.APPROVED},
    "set_sourcing": {Status.APPROVED},
    "resolve_non_catalog_line": {Status.APPROVED},
    "change_priority": {Status.APPROVED},
    "close_line_short": {Status.APPROVED},
    "reopen_line": {Status.APPROVED},
    "manual_close": {Status.APPROVED},
    "reopen": {Status.CLOSED},
}


def _operations(cable: Material) -> dict[str, Callable[[Any, MaterialRequest], Any]]:
    def line(request: MaterialRequest) -> UUID:
        return lines_of(request)[0].pk

    def free_line(request: MaterialRequest) -> UUID:
        return MaterialRequestLine.objects.get(request=request, line_no=9).pk

    def closed_line(request: MaterialRequest) -> UUID:
        MaterialRequestLine.objects.filter(pk=line(request)).update(
            closed_short_at=request.created_at,
            closed_short_by=request.requested_by,
            closed_short_source="LINE",
            close_reason="x",
        )
        return line(request)

    return {
        "decide_line": lambda p, r: services.decide_line(p, line(r), decision="APPROVED"),
        "approve_all": lambda p, r: services.approve_all(p, r.pk),
        "return_to_draft": lambda p, r: services.return_to_draft(p, r.pk, note="x"),
        "reduce_approved_qty": lambda p, r: services.reduce_approved_qty(
            p, line(r), approved_qty=D(1)
        ),
        "set_sourcing": lambda p, r: services.set_sourcing(p, line(r), sourcing="STOCK"),
        "resolve_non_catalog_line": lambda p, r: services.resolve_non_catalog_line(
            p, free_line(r), material_id=cable.pk
        ),
        "change_priority": lambda p, r: services.change_priority(p, r.pk, priority="HIGH"),
        "close_line_short": lambda p, r: services.close_line_short(p, line(r), reason="x"),
        "reopen_line": lambda p, r: services.reopen_line(p, closed_line(r), reason="x"),
        "manual_close": lambda p, r: services.manual_close(p, r.pk, reason="x"),
        "reopen": lambda p, r: services.reopen(p, r.pk, reason="x"),
    }


@pytest.mark.parametrize("status", Status.values)
@pytest.mark.parametrize("operation", sorted(ALLOWED_IN))
def test_state_rules(
    make_request: MakeRequest, people: People, cable: Material, status: str, operation: str
) -> None:
    # Lines approved in every state but DRAFT, so only the state decides.
    request = make_request(status, decided=status != Status.DRAFT)
    if status == Status.SUBMITTED:
        MaterialRequestLine.objects.filter(request=request).update(
            decision=Decision.PENDING, approved_qty=None, decided_by=None, decided_at=None
        )
    non_catalog(request)
    run = _operations(cable)[operation]
    if status in ALLOWED_IN[operation]:
        run(mm(people), request)
    else:
        with pytest.raises(ValidationError) as exc:
            run(mm(people), request)
        assert error_codes(exc) & {"transition", "not_live"}
        assert fresh(request).status == status
