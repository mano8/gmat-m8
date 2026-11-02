"""Reviewer edits and the as-submitted copy (M7b.3, Q32-Q34, Q36, ADR-0014 §5-§6).

Called without any view: the services check ``request.edit`` (and ``request.review`` for a
decision in the same step), the state, the ledger floors and the self-decision rule
themselves. The flows that purchasing and handovers contribute come from the
``flows_of`` stand-in, except where a real allocation decides between an in-place change
and a replacement.
"""

from collections.abc import Callable, Iterator
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.access.exceptions import AccessDenied, MFARequired, NotInScope
from apps.audit.models import AuditEvent
from apps.catalog.models import Material, UnitOfMeasure
from apps.core import events
from apps.core.idempotency import IdempotencyConflict, issue_key
from apps.material_requests import revisions, services
from apps.material_requests.ledger import Flows
from apps.material_requests.models import (
    MaterialRequest,
    MaterialRequestBaseline,
    MaterialRequestBaselineLine,
    MaterialRequestLine,
    MaterialRequestLineRevision,
)
from apps.material_requests.services import LineEdit
from apps.organizations.models import Branch
from apps.projects.models import Project
from tests.org import People
from tests.support import error_codes, principal_for
from tests.unit.material_requests.conftest import MakeRequest

pytestmark = pytest.mark.django_db

Status = MaterialRequest.Status
Decision = MaterialRequestLine.Decision
Sourcing = MaterialRequestLine.Sourcing
Kind = MaterialRequestLineRevision.Kind
D = Decimal


def fresh(obj: Any) -> Any:
    return type(obj).objects.get(pk=obj.pk)


def lines_of(request: MaterialRequest) -> list[MaterialRequestLine]:
    return list(MaterialRequestLine.objects.filter(request=request).order_by("line_no"))


def actions() -> list[str]:
    return list(AuditEvent.objects.order_by("occurred_at", "id").values_list("action", flat=True))


def edit(by: Any, line: MaterialRequestLine, reason: str = "Stock changed", **changes: Any) -> Any:
    return services.edit_line(principal_for(by), line.pk, LineEdit(reason, changes))


@pytest.fixture
def edited_events() -> Iterator[list[events.Event]]:
    seen: list[events.Event] = []
    unsubscribe = events.subscribe("request.edited", seen.append)
    yield seen
    unsubscribe()


@pytest.fixture
def submitted(make_request: MakeRequest, people: People) -> MaterialRequest:
    """A hospital request of A-MAD's technician with two cable lines of 10, submitted."""
    draft = make_request(lines=2, notes="For the second floor")
    return services.submit(principal_for(people.tech_mad), draft.pk)


@pytest.fixture
def approved(submitted: MaterialRequest, people: People) -> MaterialRequest:
    """``submitted``, approved in full by A-MAD's material manager."""
    return services.approve_all(principal_for(people.mm_mad), submitted.pk)


def unit(code: str) -> UnitOfMeasure:
    return UnitOfMeasure.objects.get(code=code)


# --- the as-submitted copy ------------------------------------------------------------


class TestBaseline:
    def test_every_submit_copies_the_lines(
        self, submitted: MaterialRequest, people: People, cable: Material
    ) -> None:
        (baseline,) = MaterialRequestBaseline.objects.filter(request=submitted)
        assert (baseline.sequence, baseline.submitted_by_id, baseline.backfilled) == (
            1,
            people.tech_mad.pk,
            False,
        )
        assert baseline.submitted_at == fresh(submitted).submitted_at
        assert baseline.notes == "For the second floor"
        copies = list(baseline.lines.order_by("line_no"))
        assert [(c.line_id, c.line_no) for c in copies] == [
            (line.pk, line.line_no) for line in lines_of(submitted)
        ]
        first = copies[0]
        assert (first.material_id, first.unit_id, first.requested_qty, first.free_text) == (
            cable.pk,
            cable.unit_id,
            D(10),
            "",
        )
        assert first.material_snapshot == {"reference": "CAB-25", "name": "Cable 2,5"}
        submit = AuditEvent.objects.get(action="request.submitted")
        assert submit.data["baseline"] == 1

    def test_a_non_catalog_line_keeps_its_description(
        self, make_request: MakeRequest, people: People
    ) -> None:
        tech = principal_for(people.tech_mad)
        draft = make_request(lines=0)
        services.add_line(
            tech, draft.pk, free_text="Bandeja", unit_id=unit("M").pk, requested_qty=D(4)
        )
        services.submit(tech, draft.pk)
        copy = MaterialRequestBaselineLine.objects.get(baseline__request=draft)
        assert (copy.material_id, copy.free_text, copy.material_snapshot) == (None, "Bandeja", {})

    def test_a_resubmit_starts_a_new_baseline_and_keeps_the_earlier_one(
        self, submitted: MaterialRequest, people: People
    ) -> None:
        tech = principal_for(people.tech_mad)
        services.withdraw(tech, submitted.pk)
        first, second = lines_of(submitted)
        services.update_line(tech, first.pk, requested_qty=D(12))
        services.remove_line(tech, second.pk)
        services.submit(tech, submitted.pk)
        old, new = MaterialRequestBaseline.objects.filter(request=submitted).order_by("sequence")
        assert (old.sequence, new.sequence) == (1, 2)
        assert [(c.line_no, c.requested_qty) for c in old.lines.order_by("line_no")] == [
            (1, D(10)),
            (2, D(10)),
        ]
        assert [(c.line_no, c.requested_qty) for c in new.lines.all()] == [(1, D(12))]

    @pytest.mark.parametrize(
        "model", [MaterialRequestBaseline, MaterialRequestBaselineLine, MaterialRequestLineRevision]
    )
    def test_history_rows_are_append_only(
        self, approved: MaterialRequest, people: People, model: Any
    ) -> None:
        edit(people.mm_mad, lines_of(approved)[0], requested_qty=D(12))
        rows = model.objects.all()
        assert rows.exists()
        with pytest.raises(IntegrityError, match="append-only"), transaction.atomic():
            rows.update(updated_at=rows.first().created_at)
        with pytest.raises(IntegrityError, match="append-only"), transaction.atomic():
            rows._raw_delete(rows.db)  # past Django's PROTECT: the trigger refuses it

    def test_a_copy_carries_its_materials_unit_c9(
        self, submitted: MaterialRequest, breaker: Material
    ) -> None:
        baseline = MaterialRequestBaseline.objects.get(request=submitted)
        with pytest.raises(IntegrityError), transaction.atomic():
            MaterialRequestBaselineLine.objects.create(
                baseline=baseline,
                line_id=lines_of(submitted)[0].pk,
                line_no=9,
                material=breaker,
                unit=unit("M"),
                requested_qty=D(1),
            )

    def test_a_request_submitted_before_copies_gets_one_at_its_first_edit(
        self, make_request: MakeRequest, people: People
    ) -> None:
        request = make_request(Status.SUBMITTED, lines=2)
        line = lines_of(request)[0]
        revision = edit(people.mm_mad, line, requested_qty=D(7))
        baseline = MaterialRequestBaseline.objects.get(request=request)
        assert (baseline.sequence, baseline.backfilled, baseline.submitted_by_id) == (
            1,
            True,
            people.tech_mad.pk,
        )
        assert revision.baseline_id == baseline.pk
        assert [c.requested_qty for c in baseline.lines.order_by("line_no")] == [D(10), D(10)]
        edit(people.mm_mad, line, requested_qty=D(8))
        assert MaterialRequestBaseline.objects.filter(request=request).count() == 1


# --- who may edit, and when -----------------------------------------------------------

DENIED: dict[str, tuple[str, type[Exception]]] = {
    "the requester": ("tech_mad", AccessDenied),
    "a site manager": ("site_manager", AccessDenied),
    "a warehouse operator": ("wo_mad", AccessDenied),
    "another branch": ("mm_sev", NotInScope),
    "a partner": ("partner", NotInScope),
    "revoked": ("revoked", NotInScope),
    "role held elsewhere": ("mixed", NotInScope),
}


class TestWho:
    @pytest.mark.parametrize("by", ["mm_mad", "engineer", "group_admin"])
    @pytest.mark.parametrize("state", ["submitted", "approved"])
    def test_group_admins_material_managers_and_engineers(
        self, request: pytest.FixtureRequest, people: People, by: str, state: str
    ) -> None:
        target = request.getfixturevalue(state)
        revision = edit(getattr(people, by), lines_of(target)[0], requested_qty=D(12))
        assert revision.editor_id == getattr(people, by).pk
        assert fresh(lines_of(target)[0]).requested_qty == D(12)

    @pytest.mark.parametrize("case", sorted(DENIED))
    def test_every_other_actor_is_refused(
        self, approved: MaterialRequest, people: People, case: str
    ) -> None:
        name, refusal = DENIED[case]
        line = lines_of(approved)[0]
        with pytest.raises(refusal):
            edit(getattr(people, name), line, requested_qty=D(12))
        with pytest.raises(refusal):
            services.remove_line_as_reviewer(
                principal_for(getattr(people, name)), line.pk, reason="x"
            )
        assert fresh(line).requested_qty == D(10)
        assert not MaterialRequestLineRevision.objects.exists()

    def test_an_engineer_without_session_mfa(
        self, approved: MaterialRequest, people: People
    ) -> None:
        with pytest.raises(MFARequired):
            services.edit_line(
                principal_for(people.engineer, mfa_at=None),
                lines_of(approved)[0].pk,
                LineEdit("x", {"requested_qty": D(12)}),
            )

    @pytest.mark.parametrize(
        "status", [Status.DRAFT, Status.CLOSED, Status.REJECTED, Status.CANCELLED]
    )
    def test_other_states_refuse_every_edit(
        self, make_request: MakeRequest, people: People, status: str
    ) -> None:
        request = make_request(status, decided=status == Status.CLOSED)
        line = lines_of(request)[0]
        mm = principal_for(people.mm_mad)
        attempts: list[Callable[[], Any]] = [
            lambda: edit(people.mm_mad, line, requested_qty=D(3)),
            lambda: services.add_line_as_reviewer(
                mm,
                request.pk,
                LineEdit("x", {"material_id": line.material_id, "requested_qty": D(1)}),
            ),
            lambda: services.remove_line_as_reviewer(mm, line.pk, reason="x"),
        ]
        for attempt in attempts:
            with pytest.raises(ValidationError) as exc:
                attempt()
            assert error_codes(exc) == {"transition"}

    def test_a_reason_is_required(self, approved: MaterialRequest, people: People) -> None:
        with pytest.raises(ValidationError) as exc:
            edit(people.mm_mad, lines_of(approved)[0], reason="  ", requested_qty=D(12))
        assert error_codes(exc) == {"required"}

    def test_only_line_fields(self, approved: MaterialRequest, people: People) -> None:
        with pytest.raises(ValueError, match="sourcing"):
            edit(people.mm_mad, lines_of(approved)[0], sourcing="STOCK")
        with pytest.raises(ValueError, match="approved_qty"):
            services.add_line_as_reviewer(
                principal_for(people.mm_mad), approved.pk, LineEdit("x", {"approved_qty": D(1)})
            )

    def test_a_material_manager_decides_their_own_request(
        self, make_request: MakeRequest, people: People
    ) -> None:
        """Self-deciding roles approve their own edits whatever the branch says (Q33)."""
        assert not Branch.objects.get(code="MAD", company__code="A").allow_self_approval
        draft = make_request(user=people.mm_mad)
        own = services.submit(principal_for(people.mm_mad), draft.pk)
        line = lines_of(own)[0]
        services.edit_line(
            principal_for(people.mm_mad),
            line.pk,
            LineEdit("Take 12", {"requested_qty": D(12)}, decision=Decision.APPROVED),
        )
        assert fresh(own).status == Status.APPROVED
        event = AuditEvent.objects.get(action="request_line.edited")
        assert (event.data["self_approved"], event.data["self_decided"]) == (True, True)
        assert AuditEvent.objects.get(action="request.approved").data["self_decided"] is True


# --- submitted requests ---------------------------------------------------------------


class TestEditSubmitted:
    def test_an_edit_clears_another_reviewers_decision(
        self,
        submitted: MaterialRequest,
        people: People,
        edited_events: list[events.Event],
    ) -> None:
        first, second = lines_of(submitted)
        services.decide_line(principal_for(people.site_manager), first.pk, decision="APPROVED")
        revision = edit(people.engineer, first, reason="Supplier has 12", requested_qty=D(12))
        line = fresh(first)
        assert (line.requested_qty, line.decision, line.approved_qty) == (
            D(12),
            Decision.PENDING,
            None,
        )
        assert (line.decided_by_id, line.decided_at, line.decision_note) == (None, None, "")
        assert (revision.kind, revision.line_id, revision.line_no) == (Kind.CHANGE, first.pk, 1)
        assert (revision.reason, revision.self_approved) == ("Supplier has 12", False)
        assert revision.before["requested_qty"] == "10"
        assert revision.before["decision"] == Decision.APPROVED
        assert revision.after["requested_qty"] == "12"
        assert revision.after["decision"] == Decision.PENDING
        assert fresh(submitted).status == Status.SUBMITTED
        assert actions()[-1] == "request_line.edited"
        assert [event.payload for event in edited_events] == [
            {
                "request_id": submitted.pk,
                "revision_id": revision.pk,
                "editor_id": people.engineer.pk,
            }
        ]
        assert fresh(second).decision == Decision.PENDING

    def test_the_editor_approves_the_edited_line_and_finalises(
        self, submitted: MaterialRequest, people: People
    ) -> None:
        first, second = lines_of(submitted)
        services.decide_line(principal_for(people.mm_mad), second.pk, decision="APPROVED")
        revision = services.edit_line(
            principal_for(people.engineer),
            first.pk,
            LineEdit("Take 12", {"requested_qty": D(12)}, Decision.APPROVED, D(11)),
        )
        line = fresh(first)
        assert (line.decision, line.approved_qty, line.decided_by_id) == (
            Decision.APPROVED,
            D(11),
            people.engineer.pk,
        )
        assert line.decision_note == "Take 12"
        assert revision.self_approved and revision.after["approved_qty"] == "11"
        assert fresh(submitted).status == Status.APPROVED
        assert actions()[-2:] == ["request_line.edited", "request.approved"]
        assert "self_decided" not in AuditEvent.objects.get(action="request_line.edited").data

    def test_the_editor_rejects_the_edited_line(
        self, submitted: MaterialRequest, people: People
    ) -> None:
        first, _second = lines_of(submitted)
        services.edit_line(
            principal_for(people.mm_mad),
            first.pk,
            LineEdit("Wrong size", {"requested_qty": D(3)}, Decision.REJECTED),
        )
        line = fresh(first)
        assert (line.decision, line.approved_qty, line.decision_note) == (
            Decision.REJECTED,
            None,
            "Wrong size",
        )

    @pytest.mark.parametrize(
        ("decision", "approved_qty", "code"),
        [("MAYBE", None, "invalid"), ("", D(1), "invalid"), ("APPROVED", D(20), "above_requested")],
    )
    def test_decision_values_are_checked(
        self,
        submitted: MaterialRequest,
        people: People,
        decision: str,
        approved_qty: Decimal | None,
        code: str,
    ) -> None:
        with pytest.raises(ValidationError) as exc:
            services.edit_line(
                principal_for(people.mm_mad),
                lines_of(submitted)[0].pk,
                LineEdit("x", {"requested_qty": D(12)}, decision, approved_qty),
            )
        assert error_codes(exc) == {code}

    def test_a_decision_needs_request_review(
        self, submitted: MaterialRequest, people: People, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A decision in the same step is a review: ``request.review`` is checked too."""
        from apps.material_requests.services import common, editing

        checked: list[str] = []
        real = common._authorize

        def spy(principal: Any, capability: str, *args: Any) -> Any:
            checked.append(capability)
            return real(principal, capability, *args)

        monkeypatch.setattr(editing, "_authorize", spy)
        services.edit_line(
            principal_for(people.mm_mad),
            lines_of(submitted)[0].pk,
            LineEdit("x", {"requested_qty": D(12)}, Decision.APPROVED),
        )
        assert checked == ["request.edit", "request.review"]

    def test_material_and_description_change(
        self, submitted: MaterialRequest, people: People, breaker: Material
    ) -> None:
        first, second = lines_of(submitted)
        edit(people.mm_mad, first, material_id=breaker.pk, requested_qty=D(3))
        changed = fresh(first)
        assert (changed.material_id, changed.unit_id) == (breaker.pk, breaker.unit_id)
        edit(people.mm_mad, second, material_id=None, free_text="Tubo 20", unit_id=unit("M").pk)
        described = fresh(second)
        assert (described.material_id, described.free_text) == (None, "Tubo 20")

    def test_content_is_validated(
        self, submitted: MaterialRequest, people: People, breaker: Material
    ) -> None:
        line = lines_of(submitted)[0]
        cases: list[tuple[dict[str, Any], str]] = [
            ({"requested_qty": D("1.5"), "material_id": breaker.pk}, "precision"),
            ({"material_id": breaker.pk, "unit_id": unit("M").pk}, "unit_mismatch"),
            ({"material_id": None, "free_text": ""}, "required"),
            ({"requested_qty": D(0)}, "positive"),
        ]
        for changes, code in cases:
            with pytest.raises(ValidationError) as exc:
                edit(people.mm_mad, line, **changes)
            assert error_codes(exc) == {code}
        Material.objects.filter(pk=breaker.pk).update(is_active=False)
        with pytest.raises(ValidationError) as exc:
            edit(people.mm_mad, line, material_id=breaker.pk)
        assert error_codes(exc) == {"inactive"}

    def test_an_edit_that_changes_nothing(self, submitted: MaterialRequest, people: People) -> None:
        with pytest.raises(ValidationError) as exc:
            edit(people.mm_mad, lines_of(submitted)[0], requested_qty=D("10.00"))
        assert error_codes(exc) == {"unchanged"}

    def test_add_a_line(
        self, submitted: MaterialRequest, people: People, breaker: Material
    ) -> None:
        revision = services.add_line_as_reviewer(
            principal_for(people.engineer),
            submitted.pk,
            LineEdit("Missing breakers", {"material_id": breaker.pk, "requested_qty": D(2)}),
        )
        added = MaterialRequestLine.objects.get(pk=revision.line_id)
        assert (added.line_no, added.decision, added.requested_qty) == (3, Decision.PENDING, D(2))
        assert (revision.kind, revision.before, revision.self_approved) == (Kind.ADD, {}, False)
        assert revision.after["reference"] == "DIF-40"
        assert actions()[-1] == "request_line.added_by_reviewer"

    def test_add_with_a_decision(
        self, submitted: MaterialRequest, people: People, breaker: Material
    ) -> None:
        mm = principal_for(people.mm_mad)
        services.decide_line(mm, lines_of(submitted)[0].pk, decision="REJECTED", note="No")
        revision = services.add_line_as_reviewer(
            mm,
            submitted.pk,
            LineEdit("Instead", {"material_id": breaker.pk, "requested_qty": D(2)}, "APPROVED"),
        )
        assert revision.self_approved
        assert fresh(submitted).status == Status.SUBMITTED  # line 2 is still pending

    def test_add_needs_complete_content(self, submitted: MaterialRequest, people: People) -> None:
        with pytest.raises(ValidationError) as exc:
            services.add_line_as_reviewer(
                principal_for(people.mm_mad), submitted.pk, LineEdit("x", {"free_text": "Caja"})
            )
        assert error_codes(exc) == {"required"}

    def test_remove_rejects_the_line_and_may_finalise(
        self, submitted: MaterialRequest, people: People
    ) -> None:
        first, second = lines_of(submitted)
        mm = principal_for(people.mm_mad)
        services.decide_line(mm, first.pk, decision="APPROVED")
        revision = services.remove_line_as_reviewer(
            principal_for(people.engineer), second.pk, reason="Not needed any more"
        )
        line = fresh(second)
        assert (line.decision, line.decision_note, line.decided_by_id) == (
            Decision.REJECTED,
            "Not needed any more",
            people.engineer.pk,
        )
        assert (revision.kind, revision.self_approved) == (Kind.REMOVE, True)
        assert revision.after["decision"] == Decision.REJECTED
        assert fresh(submitted).status == Status.APPROVED
        assert actions()[-2:] == ["request_line.removed_by_reviewer", "request.approved"]
        with pytest.raises(ValidationError):
            services.remove_line_as_reviewer(mm, second.pk, reason="again")

    def test_remove_an_already_rejected_line(
        self, submitted: MaterialRequest, people: People
    ) -> None:
        line = lines_of(submitted)[0]
        services.decide_line(principal_for(people.mm_mad), line.pk, decision="REJECTED", note="No")
        with pytest.raises(ValidationError) as exc:
            services.remove_line_as_reviewer(principal_for(people.mm_mad), line.pk, reason="x")
        assert error_codes(exc) == {"already_removed"}


# --- approved requests ----------------------------------------------------------------


class TestEditApproved:
    def test_a_new_quantity_is_requested_and_approved(
        self, approved: MaterialRequest, people: People, edited_events: list[events.Event]
    ) -> None:
        first = lines_of(approved)[0]
        revision = edit(people.engineer, first, requested_qty=D(14))
        line = fresh(first)
        assert (line.requested_qty, line.approved_qty, line.decision) == (
            D(14),
            D(14),
            Decision.APPROVED,
        )
        assert line.decided_by_id == people.engineer.pk
        assert (revision.kind, revision.self_approved) == (Kind.CHANGE, True)
        assert revision.before["approved_qty"] == "10"
        assert revision.after["approved_qty"] == "14"
        assert fresh(approved).status == Status.APPROVED
        assert len(edited_events) == 1

    def test_a_partially_approved_line_keeps_its_approval_without_a_new_quantity(
        self, make_request: MakeRequest, people: People, breaker: Material
    ) -> None:
        request = make_request(Status.APPROVED, decided=True)  # approved 5 of 10
        line = lines_of(request)[0]
        edit(people.mm_mad, line, material_id=breaker.pk)
        changed = fresh(line)
        assert (changed.requested_qty, changed.approved_qty) == (D(10), D(5))

    def test_kept_quantities_must_suit_a_new_unit(
        self, approved: MaterialRequest, people: People, breaker: Material
    ) -> None:
        line = lines_of(approved)[0]
        MaterialRequestLine.objects.filter(pk=line.pk).update(
            requested_qty=D("10.5"), approved_qty=D("10.5")
        )
        with pytest.raises(ValidationError) as exc:
            edit(people.mm_mad, line, material_id=breaker.pk)
        assert error_codes(exc) == {"precision"}
        edit(people.mm_mad, line, material_id=breaker.pk, requested_qty=D(10))
        assert fresh(line).unit_id == breaker.unit_id

    def test_never_below_the_commitment_floor(
        self, approved: MaterialRequest, people: People, flows_of: dict[UUID, Flows]
    ) -> None:
        first = lines_of(approved)[0]
        flows_of[first.pk] = Flows(delivered=D(2), received=D(3), pending=D(2), reserved=D(1))
        with pytest.raises(ValidationError) as exc:
            edit(people.mm_mad, first, requested_qty=D("5.99"))
        assert error_codes(exc) == {"below_commitment"}
        edit(people.mm_mad, first, requested_qty=D(6))
        assert fresh(first).approved_qty == D(6)

    def test_down_to_what_was_delivered_closes_the_request(
        self, approved: MaterialRequest, people: People, flows_of: dict[UUID, Flows]
    ) -> None:
        first, second = lines_of(approved)
        flows_of[first.pk] = Flows(delivered=D(10))
        flows_of[second.pk] = Flows(delivered=D(4))
        edit(people.mm_mad, second, requested_qty=D(4))
        stored = fresh(approved)
        assert (stored.status, stored.close_mode) == (Status.CLOSED, "AUTO")
        assert actions()[-2:] == ["request_line.edited", "request.auto_closed"]

    def test_a_material_change_in_place_clears_the_sourcing(
        self, approved: MaterialRequest, people: People, breaker: Material
    ) -> None:
        first = lines_of(approved)[0]
        services.set_sourcing(principal_for(people.mm_mad), first.pk, sourcing="PURCHASE")
        revision = edit(people.mm_mad, first, material_id=breaker.pk, requested_qty=D(4))
        line = fresh(first)
        assert (line.material_id, line.unit_id, line.sourcing) == (
            breaker.pk,
            breaker.unit_id,
            Sourcing.UNDECIDED,
        )
        assert (line.requested_qty, line.approved_qty) == (D(4), D(4))
        assert revision.kind == Kind.CHANGE
        assert MaterialRequestLine.objects.filter(request=approved).count() == 2

    def test_a_description_change_of_a_catalog_line_stays_in_place(
        self, approved: MaterialRequest, people: People, flows_of: dict[UUID, Flows]
    ) -> None:
        first = lines_of(approved)[0]
        flows_of[first.pk] = Flows(delivered=D(2))
        revision = edit(people.mm_mad, first, free_text="Red one")
        assert revision.kind == Kind.CHANGE
        assert fresh(first).free_text == "Red one"

    def test_a_material_change_after_deliveries_replaces_the_line(
        self,
        approved: MaterialRequest,
        people: People,
        breaker: Material,
        cable: Material,
        flows_of: dict[UUID, Flows],
    ) -> None:
        first = lines_of(approved)[0]
        flows_of[first.pk] = Flows(delivered=D(2), received=D(3))
        other = Material.objects.create(reference="CAB-40", name="Cable 4", unit=cable.unit)
        revision = edit(people.engineer, first, reason="Out of stock", material_id=other.pk)
        old = fresh(first)
        assert (old.closed_short_source, old.close_reason, old.closed_short_by_id) == (
            "LINE",
            "Out of stock",
            people.engineer.pk,
        )
        assert old.approved_qty == D(10)  # what it got stays on it (max(D, R) = 3)
        new = MaterialRequestLine.objects.get(pk=revision.replacement_line_id)
        assert (new.line_no, new.material_id, new.unit_id) == (3, other.pk, cable.unit_id)
        assert (new.requested_qty, new.approved_qty, new.decision) == (
            D(7),
            D(7),
            Decision.APPROVED,
        )
        assert (new.sourcing, new.decided_by_id) == (Sourcing.UNDECIDED, people.engineer.pk)
        assert (revision.kind, revision.line_id, revision.self_approved) == (
            Kind.REPLACE,
            first.pk,
            True,
        )
        assert revision.before["reference"] == "CAB-25"
        assert revision.after["reference"] == "CAB-40"
        assert revision.after["line_no"] == 3
        assert actions()[-1] == "request_line.replaced"
        assert fresh(approved).status == Status.APPROVED

    def test_a_replacement_in_another_unit_needs_its_quantity(
        self,
        approved: MaterialRequest,
        people: People,
        breaker: Material,
        flows_of: dict[UUID, Flows],
    ) -> None:
        first = lines_of(approved)[0]
        flows_of[first.pk] = Flows(delivered=D(2))
        with pytest.raises(ValidationError) as exc:
            edit(people.mm_mad, first, material_id=breaker.pk)
        assert error_codes(exc) == {"required"}
        with pytest.raises(ValidationError) as exc:
            edit(people.mm_mad, first, material_id=breaker.pk, requested_qty=D("1.5"))
        assert error_codes(exc) == {"precision"}
        revision = edit(people.mm_mad, first, material_id=breaker.pk, requested_qty=D(3))
        new = MaterialRequestLine.objects.get(pk=revision.replacement_line_id)
        assert (new.unit_id, new.approved_qty) == (breaker.unit_id, D(3))

    def test_nothing_left_to_replace(
        self, approved: MaterialRequest, people: People, breaker: Material, flows_of: Any
    ) -> None:
        first = lines_of(approved)[0]
        flows_of[first.pk] = Flows(received=D(10), delivered=D(4))
        with pytest.raises(ValidationError) as exc:
            edit(people.mm_mad, first, material_id=None, free_text="Other", unit_id=first.unit_id)
        assert error_codes(exc) == {"required"}
        flows_of[first.pk] = Flows(received=D(10), delivered=D(10))
        with pytest.raises(ValidationError) as exc:
            edit(people.mm_mad, first, material_id=breaker.pk, requested_qty=D(1))
        assert error_codes(exc) == {"fully_delivered"}

    def test_a_new_line_needs_an_open_project(
        self,
        approved: MaterialRequest,
        people: People,
        breaker: Material,
        flows_of: dict[UUID, Flows],
    ) -> None:
        first = lines_of(approved)[0]
        flows_of[first.pk] = Flows(delivered=D(1))
        Project.objects.filter(pk=approved.project_id).update(status=Project.Status.ON_HOLD)
        with pytest.raises(ValidationError) as exc:
            edit(people.mm_mad, first, material_id=breaker.pk, requested_qty=D(1))
        assert error_codes(exc) == {"project_not_active"}
        with pytest.raises(ValidationError) as exc:
            services.add_line_as_reviewer(
                principal_for(people.mm_mad),
                approved.pk,
                LineEdit("x", {"material_id": breaker.pk, "requested_qty": D(1)}),
            )
        assert error_codes(exc) == {"project_not_active"}
        edit(people.mm_mad, first, requested_qty=D(11))  # no new line: allowed

    def test_only_live_lines(self, make_request: MakeRequest, people: People) -> None:
        first, second, _third = lines_of(make_request(Status.APPROVED, lines=3, decided=True))
        mm = principal_for(people.mm_mad)
        services.close_line_short(mm, first.pk, reason="x")
        services.reduce_approved_qty(mm, second.pk, approved_qty=D(0))
        for line, code in ((first, "closed_short"), (second, "not_live")):
            with pytest.raises(ValidationError) as exc:
                edit(people.mm_mad, line, requested_qty=D(3))
            assert error_codes(exc) == {code}

    @pytest.mark.parametrize(
        ("decision", "approved_qty"), [("APPROVED", None), ("REJECTED", None), ("", D(1))]
    )
    def test_no_separate_decision(
        self, approved: MaterialRequest, people: People, decision: str, approved_qty: Any
    ) -> None:
        with pytest.raises(ValidationError) as exc:
            services.edit_line(
                principal_for(people.mm_mad),
                lines_of(approved)[0].pk,
                LineEdit("x", {"requested_qty": D(12)}, decision, approved_qty),
            )
        assert error_codes(exc) == {"approved_request"}

    def test_add_an_approved_line(
        self, approved: MaterialRequest, people: People, breaker: Material
    ) -> None:
        revision = services.add_line_as_reviewer(
            principal_for(people.group_admin),
            approved.pk,
            LineEdit("Also these", {"material_id": breaker.pk, "requested_qty": D(5)}),
        )
        added = MaterialRequestLine.objects.get(pk=revision.line_id)
        assert (added.decision, added.requested_qty, added.approved_qty) == (
            Decision.APPROVED,
            D(5),
            D(5),
        )
        assert (added.decided_by_id, added.sourcing) == (people.group_admin.pk, Sourcing.UNDECIDED)
        assert revision.self_approved

    def test_remove_closes_the_line_short(
        self, approved: MaterialRequest, people: People, flows_of: dict[UUID, Flows]
    ) -> None:
        first, second = lines_of(approved)
        flows_of[first.pk] = Flows(delivered=D(10))
        revision = services.remove_line_as_reviewer(
            principal_for(people.mm_mad), second.pk, reason="Cancelled by the client"
        )
        line = fresh(second)
        assert (line.closed_short_source, line.close_reason) == ("LINE", "Cancelled by the client")
        assert (line.decision, line.approved_qty) == (Decision.APPROVED, D(10))
        assert revision.after["closed_short"] is True
        assert fresh(approved).status == Status.CLOSED  # line 1 delivered, line 2 removed
        with pytest.raises(ValidationError) as exc:
            services.remove_line_as_reviewer(principal_for(people.mm_mad), first.pk, reason="x")
        assert error_codes(exc) == {"transition"}

    def test_remove_a_delivered_line(
        self, approved: MaterialRequest, people: People, flows_of: dict[UUID, Flows]
    ) -> None:
        first = lines_of(approved)[0]
        flows_of[first.pk] = Flows(delivered=D(10))
        with pytest.raises(ValidationError) as exc:
            services.remove_line_as_reviewer(principal_for(people.mm_mad), first.pk, reason="x")
        assert error_codes(exc) == {"fully_delivered"}

    def test_a_requester_with_self_approval_edits_their_own_request(
        self, make_request: MakeRequest, people: People
    ) -> None:
        Branch.objects.filter(code="MAD", company__code="A").update(allow_self_approval=True)
        draft = make_request(user=people.engineer)
        engineer = principal_for(people.engineer)
        own = services.approve_all(engineer, services.submit(engineer, draft.pk).pk)
        revision = edit(people.engineer, lines_of(own)[0], requested_qty=D(3))
        assert revision.self_approved
        assert AuditEvent.objects.get(action="request_line.edited").data["self_decided"] is True


# --- the version a page saw -----------------------------------------------------------


class TestSeenVersion:
    def test_it_counts_submits_and_edits(self, approved: MaterialRequest, people: People) -> None:
        assert revisions.version(approved.pk) == 1
        edit(people.mm_mad, lines_of(approved)[0], requested_qty=D(12))
        assert revisions.version(approved.pk) == 2

    def test_a_stale_page_is_refused(self, submitted: MaterialRequest, people: People) -> None:
        mm = principal_for(people.mm_mad)
        first, second = lines_of(submitted)
        seen = revisions.version(submitted.pk)
        edit(people.engineer, first, requested_qty=D(12))
        stale: list[Callable[[], Any]] = [
            lambda: services.decide_line(mm, first.pk, decision="APPROVED", seen_version=seen),
            lambda: services.approve_all(mm, submitted.pk, seen_version=seen),
            lambda: services.edit_line(
                mm, second.pk, LineEdit("x", {"requested_qty": D(2)}, seen_version=seen)
            ),
            lambda: services.remove_line_as_reviewer(mm, second.pk, reason="x", seen_version=seen),
        ]
        for attempt in stale:
            with pytest.raises(ValidationError) as exc:
                attempt()
            assert error_codes(exc) == {"edited"}
        current = revisions.version(submitted.pk)
        services.approve_all(mm, submitted.pk, seen_version=current)
        assert fresh(submitted).status == Status.APPROVED


# --- idempotency ----------------------------------------------------------------------


class TestIdempotency:
    def test_a_double_tap_records_one_revision(
        self, approved: MaterialRequest, people: People
    ) -> None:
        mm = principal_for(people.mm_mad)
        line = lines_of(approved)[0]
        key = issue_key(people.mm_mad.pk, "request_line.edit")
        change = LineEdit("x", {"requested_qty": D(12)})
        first = services.edit_line(mm, line.pk, change, idempotency_key=key)
        again = services.edit_line(mm, line.pk, change, idempotency_key=key)
        assert again.pk == first.pk
        assert MaterialRequestLineRevision.objects.count() == 1
        with pytest.raises(IdempotencyConflict):
            services.edit_line(
                mm, line.pk, LineEdit("x", {"requested_qty": D(13)}), idempotency_key=key
            )

    def test_add_and_remove(self, approved: MaterialRequest, people: People, breaker: Any) -> None:
        mm = principal_for(people.mm_mad)
        add_key = issue_key(people.mm_mad.pk, "request_line.add_as_reviewer")
        content = LineEdit("x", {"material_id": breaker.pk, "requested_qty": D(1)})
        added = services.add_line_as_reviewer(mm, approved.pk, content, idempotency_key=add_key)
        assert (
            services.add_line_as_reviewer(mm, approved.pk, content, idempotency_key=add_key).pk
            == added.pk
        )
        remove_key = issue_key(people.mm_mad.pk, "request_line.remove_as_reviewer")
        line = lines_of(approved)[0]
        removed = services.remove_line_as_reviewer(
            mm, line.pk, reason="x", idempotency_key=remove_key
        )
        again = services.remove_line_as_reviewer(
            mm, line.pk, reason="x", idempotency_key=remove_key
        )
        assert again.pk == removed.pk
        assert MaterialRequestLineRevision.objects.count() == 2
