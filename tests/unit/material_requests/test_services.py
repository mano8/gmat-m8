"""Request lifecycle services, the requester's side (plan §4.3, §4.4, §4.7, §4.8).

Called without any view: the services enforce the capabilities, the scope and the editing
rules themselves.
"""

from collections.abc import Callable, Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.db import transaction

from apps.access.exceptions import AccessDenied, MFARequired, NotInScope
from apps.access.roles import Role
from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.catalog.models import Material, UnitOfMeasure
from apps.core import events
from apps.core.db import db_now
from apps.core.idempotency import IdempotencyConflict, issue_key
from apps.core.ids import new_id
from apps.core.locking import LockOrder, LockSetChanged
from apps.core.models import DocumentSequence
from apps.material_requests import services
from apps.material_requests.models import MaterialRequest, MaterialRequestLine
from apps.organizations.models import Branch
from apps.projects.models import Project
from tests.org import Org, People
from tests.support import error_codes, principal_for
from tests.unit.material_requests.conftest import MakeRequest

pytestmark = pytest.mark.django_db

Status = MaterialRequest.Status
DENIED = (AccessDenied, NotInScope)


def actions() -> list[str]:
    return list(AuditEvent.objects.order_by("occurred_at", "id").values_list("action", flat=True))


@pytest.fixture
def published() -> Iterator[list[events.Event]]:
    seen: list[events.Event] = []
    names = [
        "request.created",
        "request.updated",
        "request.submitted",
        "request.withdrawn",
        "request.cancelled",
        "request.discarded",
        "request_line.added",
        "request_line.updated",
        "request_line.removed",
    ]
    undo = [events.subscribe(name, seen.append) for name in names]
    yield seen
    for unsubscribe in undo:
        unsubscribe()


def clock() -> Any:
    with transaction.atomic():
        return db_now()


def year() -> int:
    return int(clock().year)


# --- create_draft ---------------------------------------------------------------------


class TestCreateDraft:
    def test_a_technician_starts_a_draft(
        self, org: Org, people: People, published: list[events.Event]
    ) -> None:
        request = services.create_draft(
            principal_for(people.tech_mad),
            project_id=org.hospital.pk,
            priority=MaterialRequest.Priority.HIGH,
            needed_by=date(2026, 12, 1),
            notes="  Planta 2  ",
        )
        assert (request.status, request.number, request.notes) == (Status.DRAFT, None, "Planta 2")
        assert request.procurement_branch_id == org.a_mad.pk  # A-MAD buys for B-MAD's project
        assert request.requested_by_id == people.tech_mad.pk
        assert actions() == ["request.created"]
        assert [event.name for event in published] == ["request.created"]

    def test_a_key_makes_it_idempotent(self, org: Org, people: People) -> None:
        principal = principal_for(people.tech_mad)
        key = issue_key(people.tech_mad.pk, "request.create")
        first = services.create_draft(principal, project_id=org.hospital.pk, idempotency_key=key)
        again = services.create_draft(principal, project_id=org.hospital.pk, idempotency_key=key)
        assert again.pk == first.pk
        assert MaterialRequest.objects.count() == 1
        with pytest.raises(IdempotencyConflict):
            services.create_draft(
                principal, project_id=org.sev_for_mad.pk, idempotency_key=key, notes="other"
            )

    @pytest.mark.parametrize(
        ("person", "project"),
        [
            ("tech_mad", "hospital"),
            ("tech_mad", "sev_for_mad"),  # A-MAD buys for it (S3)
            ("site_manager", "hospital"),
            ("partner", "hospital"),
            ("mm_mad", "hospital"),
            ("mm_sev", "sev_works"),
            ("group_admin", "sev_works"),
            ("mixed", "hospital"),  # as A-MAD technician
        ],
    )
    def test_allowed(self, org: Org, people: People, person: str, project: str) -> None:
        user, target = getattr(people, person), getattr(org, project)
        request = services.create_draft(principal_for(user), project_id=target.pk)
        assert request.project_id == target.pk

    @pytest.mark.parametrize(
        ("person", "project", "error"),
        [
            ("tech_mad", "sev_works", NotInScope),
            ("tech_mad", "mad_hold", NotInScope),  # technicians see active projects only
            ("partner", "sev_works", NotInScope),
            ("site_manager", "sev_works", NotInScope),
            ("mm_sev", "hospital", NotInScope),
            ("wo_mad", "hospital", AccessDenied),  # sees the project, may not request
            ("expired", "hospital", NotInScope),
            ("revoked", "hospital", NotInScope),
            ("revoked_assignment", "hospital", NotInScope),
            ("inactive_partner", "hospital", NotInScope),
            ("inactive_branch", "hospital", NotInScope),
            ("deactivated", "hospital", AccessDenied),
            ("nobody", "hospital", NotInScope),
            ("operator", "hospital", NotInScope),  # platform operators have no business role
        ],
    )
    def test_denied(
        self, org: Org, people: People, person: str, project: str, error: type[Exception]
    ) -> None:
        with pytest.raises(error):
            services.create_draft(
                principal_for(getattr(people, person)), project_id=getattr(org, project).pk
            )
        assert not MaterialRequest.objects.exists()

    def test_privileged_grants_need_session_mfa(self, org: Org, people: People) -> None:
        for user in (people.group_admin, people.mm_mad):
            with pytest.raises(MFARequired):
                services.create_draft(principal_for(user, mfa_at=None), project_id=org.hospital.pk)
        # The mixed user's technician grant covers A-MAD's projects without MFA (§3.6).
        services.create_draft(principal_for(people.mixed, mfa_at=None), project_id=org.hospital.pk)

    def test_a_project_on_hold_takes_no_request(self, org: Org, people: People) -> None:
        with pytest.raises(ValidationError) as exc:
            services.create_draft(principal_for(people.group_admin), project_id=org.mad_hold.pk)
        assert error_codes(exc) == {"project_not_active"}

    @pytest.mark.parametrize("branch", ["b_mad", "a_mad"])
    def test_an_inactive_branch_takes_no_request(
        self, org: Org, people: People, branch: str
    ) -> None:
        Branch.objects.filter(pk=getattr(org, branch).pk).update(is_active=False)
        with pytest.raises(ValidationError) as exc:
            services.create_draft(principal_for(people.group_admin), project_id=org.hospital.pk)
        assert error_codes(exc) == {"inactive"}

    def test_an_unknown_priority_is_refused(self, org: Org, people: People) -> None:
        with pytest.raises(ValidationError):
            services.create_draft(
                principal_for(people.tech_mad), project_id=org.hospital.pk, priority="NOW"
            )


# --- update_request -------------------------------------------------------------------


class TestUpdateRequest:
    def test_a_draft_header_changes(self, make_request: MakeRequest, people: People) -> None:
        draft = make_request()
        changed = services.update_request(
            principal_for(people.tech_mad),
            draft.pk,
            priority=MaterialRequest.Priority.URGENT,
            needed_by=date(2026, 11, 20),
            notes=" Urgente ",
        )
        assert (changed.priority, changed.needed_by, changed.notes) == (
            "URGENT",
            date(2026, 11, 20),
            "Urgente",
        )
        event = AuditEvent.objects.get(action="request.updated")
        assert set(event.data["changes"]) == {"priority", "needed_by", "notes"}

    def test_nothing_changed_writes_nothing(
        self, make_request: MakeRequest, people: People
    ) -> None:
        draft = make_request()
        services.update_request(principal_for(people.tech_mad), draft.pk, notes="")
        assert actions() == []

    def test_moving_a_draft_to_another_project(
        self, make_request: MakeRequest, org: Org, people: People
    ) -> None:
        draft = make_request(user=people.group_admin, lines=2)
        moved = services.update_request(
            principal_for(people.group_admin), draft.pk, project_id=org.sev_works.pk
        )
        assert moved.project_id == org.sev_works.pk
        assert moved.procurement_branch_id == org.a_sev.pk  # the new project's buyer
        assert set(draft.lines.values_list("project_id", flat=True)) == {org.sev_works.pk}

    def test_the_new_project_is_authorized_and_open(
        self, make_request: MakeRequest, org: Org, people: People
    ) -> None:
        draft = make_request()
        with pytest.raises(NotInScope):
            services.update_request(
                principal_for(people.tech_mad), draft.pk, project_id=org.sev_works.pk
            )
        own = make_request(user=people.group_admin)
        with pytest.raises(ValidationError) as exc:
            services.update_request(
                principal_for(people.group_admin), own.pk, project_id=org.mad_hold.pk
            )
        assert error_codes(exc) == {"project_not_active"}
        assert MaterialRequest.objects.get(pk=own.pk).project_id == org.hospital.pk

    def test_approved_requests_take_notes_only(
        self, make_request: MakeRequest, people: People
    ) -> None:
        approved = make_request(Status.APPROVED, decided=True)
        principal = principal_for(people.tech_mad)
        assert services.update_request(principal, approved.pk, notes="Llamar").notes == "Llamar"
        with pytest.raises(ValidationError) as exc:
            services.update_request(principal, approved.pk, priority="LOW")
        assert error_codes(exc) == {"not_editable"}

    @pytest.mark.parametrize(
        "status", [Status.SUBMITTED, Status.REJECTED, Status.CANCELLED, Status.CLOSED]
    )
    def test_other_states_are_read_only(
        self, make_request: MakeRequest, people: People, status: str
    ) -> None:
        request = make_request(status)
        with pytest.raises(ValidationError) as exc:
            services.update_request(principal_for(people.tech_mad), request.pk, notes="x")
        assert error_codes(exc) == {"not_editable"}

    def test_fields_outside_the_header_are_refused(
        self, make_request: MakeRequest, people: People
    ) -> None:
        with pytest.raises(ValidationError) as exc:
            services.update_request(
                principal_for(people.tech_mad), make_request().pk, status=Status.APPROVED
            )
        assert error_codes(exc) == {"not_editable"}

    def test_only_the_requester(self, make_request: MakeRequest, people: People) -> None:
        draft = make_request()
        with pytest.raises(AccessDenied):  # sees it, may not edit it
            services.update_request(principal_for(people.mm_mad), draft.pk, notes="x")
        with pytest.raises(NotInScope):  # does not even see it
            services.update_request(principal_for(people.partner), draft.pk, notes="x")


# --- lines ----------------------------------------------------------------------------


class TestAddLine:
    def test_a_catalog_line_takes_its_materials_unit(
        self, make_request: MakeRequest, people: People, cable: Material
    ) -> None:
        draft = make_request()
        line = services.add_line(
            principal_for(people.tech_mad),
            draft.pk,
            material_id=cable.pk,
            requested_qty=Decimal("25.50"),
        )
        assert (line.line_no, line.unit_id, line.project_id) == (2, cable.unit_id, draft.project_id)
        assert line.decision == MaterialRequestLine.Decision.PENDING
        assert actions() == ["request_line.added"]
        same_unit = services.add_line(
            principal_for(people.tech_mad),
            draft.pk,
            material_id=cable.pk,
            unit_id=cable.unit_id,
            requested_qty=Decimal("1"),
        )
        assert same_unit.line_no == 3

    def test_a_non_catalog_line(self, make_request: MakeRequest, people: People) -> None:
        unit = UnitOfMeasure.objects.get(code="CAJA")
        line = services.add_line(
            principal_for(people.partner),
            make_request(user=people.partner, lines=0).pk,
            free_text="  Bornas Wago 221-413 ",
            unit_id=unit.pk,
            requested_qty=Decimal("3"),
        )
        assert (line.material_id, line.free_text, line.unit_id) == (
            None,
            "Bornas Wago 221-413",
            unit.pk,
        )
        assert line.line_no == 1

    @pytest.mark.parametrize(
        ("quantity", "code"),
        [
            (Decimal("1.005"), "precision"),  # metres: two decimals
            (Decimal("0"), "positive"),
            (Decimal("-2"), "positive"),
            (Decimal("NaN"), "invalid"),
            (2.5, "invalid"),
            ("2.5", "invalid"),
        ],
    )
    def test_quantities(
        self,
        make_request: MakeRequest,
        people: People,
        cable: Material,
        quantity: Any,
        code: str,
    ) -> None:
        with pytest.raises(ValidationError) as exc:
            services.add_line(
                principal_for(people.tech_mad),
                make_request().pk,
                material_id=cable.pk,
                requested_qty=quantity,
            )
        assert error_codes(exc) == {code}

    def test_units_without_decimals(
        self, make_request: MakeRequest, people: People, breaker: Material
    ) -> None:
        principal, draft = principal_for(people.tech_mad), make_request()
        with pytest.raises(ValidationError) as exc:
            services.add_line(
                principal, draft.pk, material_id=breaker.pk, requested_qty=Decimal("2.5")
            )
        assert error_codes(exc) == {"precision"}
        line = services.add_line(
            principal, draft.pk, material_id=breaker.pk, requested_qty=Decimal("2.000")
        )
        assert line.requested_qty == Decimal("2")

    def test_too_many_digits(
        self, make_request: MakeRequest, people: People, breaker: Material
    ) -> None:
        with pytest.raises(ValidationError):
            services.add_line(
                principal_for(people.tech_mad),
                make_request().pk,
                material_id=breaker.pk,
                requested_qty=Decimal("10") ** 12,
            )

    def test_material_rules(
        self, make_request: MakeRequest, people: People, cable: Material, breaker: Material
    ) -> None:
        principal, draft = principal_for(people.tech_mad), make_request()
        with pytest.raises(ValidationError) as exc:
            services.add_line(
                principal,
                draft.pk,
                material_id=cable.pk,
                unit_id=breaker.unit_id,
                requested_qty=Decimal("1"),
            )
        assert error_codes(exc) == {"unit_mismatch"}
        Material.objects.filter(pk=cable.pk).update(is_active=False)
        with pytest.raises(ValidationError) as exc:
            services.add_line(principal, draft.pk, material_id=cable.pk, requested_qty=Decimal("1"))
        assert error_codes(exc) == {"inactive"}
        with pytest.raises(ValidationError) as exc:
            services.add_line(principal, draft.pk, material_id=new_id(), requested_qty=Decimal("1"))
        assert error_codes(exc) == {"inactive"}

    def test_non_catalog_lines_need_a_text_and_a_unit(
        self, make_request: MakeRequest, people: People
    ) -> None:
        principal, draft = principal_for(people.tech_mad), make_request()
        with pytest.raises(ValidationError) as exc:
            services.add_line(principal, draft.pk, free_text="  ", requested_qty=Decimal("1"))
        assert error_codes(exc) == {"required"}
        with pytest.raises(ValidationError) as exc:
            services.add_line(principal, draft.pk, free_text="Algo", requested_qty=Decimal("1"))
        assert error_codes(exc) == {"required"}
        with pytest.raises(ValidationError) as exc:
            services.add_line(
                principal, draft.pk, free_text="Algo", unit_id=new_id(), requested_qty=Decimal("1")
            )
        assert error_codes(exc) == {"required"}

    def test_a_key_makes_it_idempotent(
        self, make_request: MakeRequest, people: People, cable: Material
    ) -> None:
        principal, draft = principal_for(people.tech_mad), make_request(lines=0)
        key = issue_key(people.tech_mad.pk, "request_line.add")
        arguments: dict[str, Any] = {"material_id": cable.pk, "requested_qty": Decimal("4")}
        first = services.add_line(principal, draft.pk, idempotency_key=key, **arguments)
        again = services.add_line(principal, draft.pk, idempotency_key=key, **arguments)
        assert again.pk == first.pk
        assert draft.lines.count() == 1

    @pytest.mark.parametrize(
        "status",
        [Status.SUBMITTED, Status.APPROVED, Status.REJECTED, Status.CANCELLED, Status.CLOSED],
    )
    def test_only_drafts(
        self, make_request: MakeRequest, people: People, cable: Material, status: str
    ) -> None:
        request = make_request(status)
        with pytest.raises(ValidationError) as exc:
            services.add_line(
                principal_for(people.tech_mad),
                request.pk,
                material_id=cable.pk,
                requested_qty=Decimal("1"),
            )
        assert error_codes(exc) == {"not_draft"}

    @pytest.mark.parametrize(
        ("person", "error"),
        [
            ("mm_mad", AccessDenied),
            ("wo_mad", AccessDenied),
            ("site_manager", AccessDenied),
            ("group_admin", AccessDenied),
            ("partner", NotInScope),
            ("mm_sev", NotInScope),
            ("expired", NotInScope),
        ],
    )
    def test_only_the_requester(
        self,
        make_request: MakeRequest,
        people: People,
        cable: Material,
        person: str,
        error: type[Exception],
    ) -> None:
        draft = make_request()
        with pytest.raises(error):
            services.add_line(
                principal_for(getattr(people, person)),
                draft.pk,
                material_id=cable.pk,
                requested_qty=Decimal("1"),
            )
        assert draft.lines.count() == 1

    def test_a_revoked_requester_loses_their_draft(
        self, make_request: MakeRequest, people: People, cable: Material
    ) -> None:
        draft = make_request()
        people.tech_mad.memberships.update(revoked_at=clock())
        with pytest.raises(DENIED):
            services.add_line(
                principal_for(people.tech_mad),
                draft.pk,
                material_id=cable.pk,
                requested_qty=Decimal("1"),
            )


class TestUpdateLine:
    def test_quantity(self, make_request: MakeRequest, people: People) -> None:
        line = make_request().lines.get()
        changed = services.update_line(
            principal_for(people.tech_mad), line.pk, requested_qty=Decimal("12.5")
        )
        assert changed.requested_qty == Decimal("12.5")
        event = AuditEvent.objects.get(action="request_line.updated")
        assert set(event.data["changes"]) == {"requested_qty"}

    def test_the_unit_follows_a_new_material(
        self, make_request: MakeRequest, people: People, breaker: Material
    ) -> None:
        line = make_request().lines.get()
        changed = services.update_line(
            principal_for(people.tech_mad), line.pk, material_id=breaker.pk
        )
        assert (changed.material_id, changed.unit_id) == (breaker.pk, breaker.unit_id)

    def test_to_a_non_catalog_line_and_back(
        self, make_request: MakeRequest, people: People, cable: Material
    ) -> None:
        principal, line = principal_for(people.tech_mad), make_request().lines.get()
        unit = UnitOfMeasure.objects.get(code="ROLLO")
        free = services.update_line(
            principal, line.pk, material_id=None, free_text="Cable especial", unit_id=unit.pk
        )
        assert (free.material_id, free.unit_id) == (None, unit.pk)
        back = services.update_line(
            principal, line.pk, material_id=cable.pk, requested_qty=Decimal("3")
        )
        assert (back.material_id, back.unit_id) == (cable.pk, cable.unit_id)

    def test_a_deactivated_material_stays_but_cannot_be_chosen(
        self, make_request: MakeRequest, people: People, cable: Material, breaker: Material
    ) -> None:
        principal, line = principal_for(people.tech_mad), make_request().lines.get()
        Material.objects.filter(pk__in=[cable.pk, breaker.pk]).update(is_active=False)
        services.update_line(principal, line.pk, requested_qty=Decimal("2"))
        with pytest.raises(ValidationError) as exc:
            services.update_line(principal, line.pk, material_id=breaker.pk)
        assert error_codes(exc) == {"inactive"}

    def test_nothing_changed_writes_nothing(
        self, make_request: MakeRequest, people: People
    ) -> None:
        line = make_request().lines.get()
        services.update_line(
            principal_for(people.tech_mad), line.pk, requested_qty=Decimal("10.000")
        )
        assert actions() == []

    def test_other_fields_are_refused(self, make_request: MakeRequest, people: People) -> None:
        line = make_request().lines.get()
        with pytest.raises(ValueError):
            services.update_line(principal_for(people.tech_mad), line.pk, approved_qty=1)

    def test_only_drafts_and_the_requester(self, make_request: MakeRequest, people: People) -> None:
        submitted = make_request(Status.SUBMITTED).lines.get()
        with pytest.raises(ValidationError) as exc:
            services.update_line(
                principal_for(people.tech_mad), submitted.pk, requested_qty=Decimal("1")
            )
        assert error_codes(exc) == {"not_draft"}
        draft = make_request().lines.get()
        with pytest.raises(AccessDenied):
            services.update_line(principal_for(people.mm_mad), draft.pk, requested_qty=Decimal("1"))


class TestRemoveLine:
    def test_a_draft_line_is_removed(
        self, make_request: MakeRequest, people: People, cable: Material
    ) -> None:
        principal, draft = principal_for(people.tech_mad), make_request(lines=2)
        first = draft.lines.get(line_no=1)
        services.remove_line(principal, first.pk)
        assert list(draft.lines.values_list("line_no", flat=True)) == [2]
        assert AuditEvent.objects.get(action="request_line.removed").data["line_no"] == 1
        added = services.add_line(
            principal, draft.pk, material_id=cable.pk, requested_qty=Decimal("1")
        )
        assert added.line_no == 3

    def test_only_drafts_and_the_requester(self, make_request: MakeRequest, people: People) -> None:
        submitted = make_request(Status.SUBMITTED).lines.get()
        with pytest.raises(ValidationError) as exc:
            services.remove_line(principal_for(people.tech_mad), submitted.pk)
        assert error_codes(exc) == {"not_draft"}
        draft = make_request().lines.get()
        with pytest.raises(NotInScope):
            services.remove_line(principal_for(people.partner), draft.pk)
        assert MaterialRequestLine.objects.count() == 2


# --- submit ---------------------------------------------------------------------------


class TestSubmit:
    def test_number_snapshot_and_time(
        self, make_request: MakeRequest, org: Org, people: People, published: list[events.Event]
    ) -> None:
        draft = make_request()
        # The project's procurement branch changed after the draft started: submit takes it.
        Project.objects.filter(pk=org.hospital.pk).update(procurement_branch=org.b_mad)
        submitted = services.submit(principal_for(people.tech_mad), draft.pk)
        assert submitted.status == Status.SUBMITTED
        assert submitted.number == f"SOL-B-MAD-{year()}-00001"
        assert submitted.procurement_branch_id == org.b_mad.pk
        assert submitted.submitted_at is not None
        assert actions() == ["request.submitted"]
        assert [event.name for event in published] == ["request.submitted"]

    def test_numbers_per_procurement_branch_and_year(
        self, make_request: MakeRequest, org: Org, people: People
    ) -> None:
        tech, mm_sev = principal_for(people.tech_mad), principal_for(people.mm_sev)
        first = services.submit(tech, make_request().pk)
        second = services.submit(tech, make_request().pk)
        sevilla = services.submit(
            mm_sev, make_request(user=people.mm_sev, project=org.sev_works).pk
        )
        assert [first.number, second.number, sevilla.number] == [
            f"SOL-A-MAD-{year()}-00001",
            f"SOL-A-MAD-{year()}-00002",
            f"SOL-A-SEV-{year()}-00001",
        ]

    def test_a_withdrawn_request_keeps_its_number(
        self, make_request: MakeRequest, people: People
    ) -> None:
        principal, draft = principal_for(people.tech_mad), make_request()
        number = services.submit(principal, draft.pk).number
        services.withdraw(principal, draft.pk)
        assert services.submit(principal, draft.pk).number == number
        assert DocumentSequence.objects.get().next_value == 2

    def test_a_key_makes_a_double_tap_harmless(
        self, make_request: MakeRequest, people: People
    ) -> None:
        principal, draft = principal_for(people.tech_mad), make_request()
        key = issue_key(people.tech_mad.pk, "request.submit")
        first = services.submit(principal, draft.pk, idempotency_key=key)
        again = services.submit(principal, draft.pk, idempotency_key=key)
        assert (again.pk, again.number) == (first.pk, first.number)
        assert actions() == ["request.submitted"]
        with pytest.raises(IdempotencyConflict):
            services.submit(principal, make_request().pk, idempotency_key=key)
        with pytest.raises(ValidationError) as exc:  # without a key it is a second submit
            services.submit(principal, draft.pk)
        assert error_codes(exc) == {"transition"}

    def test_an_empty_draft_is_refused(self, make_request: MakeRequest, people: People) -> None:
        with pytest.raises(ValidationError) as exc:
            services.submit(principal_for(people.tech_mad), make_request(lines=0).pk)
        assert error_codes(exc) == {"empty"}
        assert not DocumentSequence.objects.exists()

    def test_a_project_put_on_hold_takes_no_submit(
        self, make_request: MakeRequest, org: Org, people: People
    ) -> None:
        draft = make_request()
        Project.objects.filter(pk=org.hospital.pk).update(status=Project.Status.ON_HOLD)
        with pytest.raises(ValidationError) as exc:
            services.submit(principal_for(people.tech_mad), draft.pk)
        assert error_codes(exc) == {"project_not_active"}

    def test_a_project_moved_out_of_reach_hides_the_draft(
        self, make_request: MakeRequest, org: Org, people: People, member: Callable[..., User]
    ) -> None:
        """The project moved to branches the technician does not belong to (404)."""
        tech = member(Role.TECHNICIAN, branch=org.b_mad)
        draft = make_request(user=tech)
        Project.objects.filter(pk=org.hospital.pk).update(branch=org.a_sev)
        with pytest.raises(NotInScope):
            services.submit(principal_for(tech), draft.pk)

    @pytest.mark.parametrize("person", ["mm_mad", "group_admin", "site_manager"])
    def test_only_the_requester(
        self, make_request: MakeRequest, people: People, person: str
    ) -> None:
        with pytest.raises(AccessDenied):
            services.submit(principal_for(getattr(people, person)), make_request().pk)


# --- withdraw and cancel --------------------------------------------------------------


class TestWithdraw:
    def test_back_to_draft_before_any_decision(
        self, make_request: MakeRequest, people: People
    ) -> None:
        submitted = make_request(Status.SUBMITTED)
        submitted.lines.update(sourcing=MaterialRequestLine.Sourcing.STOCK)
        draft = services.withdraw(principal_for(people.tech_mad), submitted.pk)
        assert (draft.status, draft.number) == (Status.DRAFT, submitted.number)
        assert draft.lines.get().sourcing == MaterialRequestLine.Sourcing.UNDECIDED
        assert actions() == ["request.withdrawn"]

    def test_a_decided_line_blocks_it(self, make_request: MakeRequest, people: People) -> None:
        submitted = make_request(Status.SUBMITTED, decided=True)
        with pytest.raises(ValidationError) as exc:
            services.withdraw(principal_for(people.tech_mad), submitted.pk)
        assert error_codes(exc) == {"decided"}

    @pytest.mark.parametrize(
        "status", [Status.DRAFT, Status.APPROVED, Status.REJECTED, Status.CANCELLED, Status.CLOSED]
    )
    def test_only_submitted_requests(
        self, make_request: MakeRequest, people: People, status: str
    ) -> None:
        with pytest.raises(ValidationError) as exc:
            services.withdraw(principal_for(people.tech_mad), make_request(status).pk)
        assert error_codes(exc) == {"transition"}

    @pytest.mark.parametrize(
        ("person", "error"),
        [("mm_mad", AccessDenied), ("wo_mad", AccessDenied), ("partner", NotInScope)],
    )
    def test_only_the_requester(
        self, make_request: MakeRequest, people: People, person: str, error: type[Exception]
    ) -> None:
        with pytest.raises(error):
            services.withdraw(
                principal_for(getattr(people, person)), make_request(Status.SUBMITTED).pk
            )


class TestCancel:
    def test_discard_an_own_draft(self, make_request: MakeRequest, people: People) -> None:
        cancelled = services.cancel(principal_for(people.tech_mad), make_request().pk)
        assert (cancelled.status, cancelled.number) == (Status.CANCELLED, None)
        assert actions() == ["request.discarded"]

    def test_cancel_an_own_submitted_request(
        self, make_request: MakeRequest, people: People
    ) -> None:
        submitted = make_request(Status.SUBMITTED)
        cancelled = services.cancel(principal_for(people.tech_mad), submitted.pk, reason=" Ya no ")
        assert cancelled.status == Status.CANCELLED
        event = AuditEvent.objects.get(action="request.cancelled")
        assert (event.data["from_status"], event.data["reason"]) == ("SUBMITTED", "Ya no")

    def test_after_a_decision_only_cancel_any(
        self, make_request: MakeRequest, people: People
    ) -> None:
        decided = make_request(Status.SUBMITTED, decided=True)
        with pytest.raises(AccessDenied):
            services.cancel(principal_for(people.tech_mad), decided.pk)
        assert services.cancel(principal_for(people.mm_mad), decided.pk).status == Status.CANCELLED

    def test_an_approved_request(self, make_request: MakeRequest, people: People) -> None:
        approved = make_request(Status.APPROVED, decided=True)
        with pytest.raises(AccessDenied):
            services.cancel(principal_for(people.tech_mad), approved.pk)
        with pytest.raises(NotInScope):
            services.cancel(principal_for(people.mm_sev), approved.pk)
        with pytest.raises(AccessDenied):
            services.cancel(principal_for(people.site_manager), approved.pk)
        cancelled = services.cancel(principal_for(people.group_admin), approved.pk, reason="Error")
        assert cancelled.status == Status.CANCELLED

    def test_someone_elses_draft_is_the_requesters(
        self, make_request: MakeRequest, people: People
    ) -> None:
        with pytest.raises(AccessDenied):
            services.cancel(principal_for(people.mm_mad), make_request().pk)

    @pytest.mark.parametrize("status", [Status.REJECTED, Status.CANCELLED, Status.CLOSED])
    def test_final_and_closed_requests(
        self, make_request: MakeRequest, people: People, status: str
    ) -> None:
        with pytest.raises(ValidationError) as exc:
            services.cancel(principal_for(people.mm_mad), make_request(status).pk)
        assert error_codes(exc) == {"transition"}

    def test_a_decision_landing_before_the_lock_is_seen(
        self, make_request: MakeRequest, people: People, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The path chosen before the lock is checked again under it.

        Outside a test's transaction the retry wrapper runs the service again, which then
        takes ``request.cancel_any`` (the integration races cover the retry).
        """
        submitted = make_request(Status.SUBMITTED)
        calls = iter([False])
        monkeypatch.setattr(services, "_decided", lambda request: next(calls, True))
        with pytest.raises(LockSetChanged):
            services.cancel(principal_for(people.tech_mad), submitted.pk)
        assert MaterialRequest.objects.get(pk=submitted.pk).status == Status.SUBMITTED


def test_a_header_changed_since_authorization_is_retried(
    make_request: MakeRequest, people: People
) -> None:
    stale = make_request()
    MaterialRequest.objects.filter(pk=stale.pk).update(status=Status.CANCELLED)
    with pytest.raises(LockSetChanged), transaction.atomic():
        services._lock_header(LockOrder(), stale)


# --- the state rules, service by service ----------------------------------------------

# Which requester operation each state allows (plan §4.4), for an actor that holds every
# capability on its own request (a group administrator with session MFA).
ALLOWED_IN = {
    "update_header": {Status.DRAFT},
    "update_notes": {Status.DRAFT, Status.APPROVED},
    "add_line": {Status.DRAFT},
    "update_line": {Status.DRAFT},
    "remove_line": {Status.DRAFT},
    "submit": {Status.DRAFT},
    "withdraw": {Status.SUBMITTED},
    "cancel": {Status.DRAFT, Status.SUBMITTED, Status.APPROVED},
}


def _operations(cable: Material) -> dict[str, Callable[[Any, MaterialRequest], Any]]:
    line = lambda request: request.lines.get().pk  # noqa: E731
    return {
        "update_header": lambda p, r: services.update_request(p, r.pk, priority="LOW"),
        "update_notes": lambda p, r: services.update_request(p, r.pk, notes="x"),
        "add_line": lambda p, r: services.add_line(
            p, r.pk, material_id=cable.pk, requested_qty=Decimal("1")
        ),
        "update_line": lambda p, r: services.update_line(p, line(r), requested_qty=Decimal("2")),
        "remove_line": lambda p, r: services.remove_line(p, line(r)),
        "submit": lambda p, r: services.submit(p, r.pk),
        "withdraw": lambda p, r: services.withdraw(p, r.pk),
        "cancel": lambda p, r: services.cancel(p, r.pk),
    }


@pytest.mark.parametrize("status", Status.values)
@pytest.mark.parametrize("operation", sorted(ALLOWED_IN))
def test_state_rules(
    make_request: MakeRequest, people: People, cable: Material, status: str, operation: str
) -> None:
    request = make_request(status, user=people.group_admin)
    run = _operations(cable)[operation]
    principal = principal_for(people.group_admin)
    if status in ALLOWED_IN[operation]:
        run(principal, request)
    else:
        with pytest.raises(ValidationError) as exc:
            run(principal, request)
        assert error_codes(exc) & {"transition", "not_draft", "not_editable"}
        after = MaterialRequest.objects.get(pk=request.pk)
        assert (after.status, after.priority, after.notes) == (status, "NORMAL", "")
