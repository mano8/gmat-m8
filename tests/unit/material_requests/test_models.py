"""Request models: constraints, composite keys (C3, C9) and scope (S5)."""

from collections.abc import Callable
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.access.context import AccessContext
from apps.access.roles import Capability, Role
from apps.accounts.models import User
from apps.catalog.models import Material, UnitOfMeasure
from apps.material_requests.models import MaterialRequest, MaterialRequestLine
from tests.org import Org, People
from tests.unit.material_requests.conftest import MakeRequest

pytestmark = pytest.mark.django_db

Status = MaterialRequest.Status


def violates(name: str, write: Callable[[], object], *alternatives: str) -> None:
    """``write`` fails on ``name`` (or on one of the other constraints it also breaks)."""
    with pytest.raises(IntegrityError) as exc, transaction.atomic():
        write()
    assert any(candidate in str(exc.value) for candidate in (name, *alternatives))


def line_update(line: MaterialRequestLine, **values: object) -> Callable[[], int]:
    return lambda: MaterialRequestLine.objects.filter(pk=line.pk).update(**values)


class TestRequestConstraints:
    def test_a_submitted_request_has_a_number_and_a_submit_time(
        self, make_request: MakeRequest
    ) -> None:
        draft = make_request()
        rows = MaterialRequest.objects.filter(pk=draft.pk)
        violates("mr_request_submitted_has_number", lambda: rows.update(status=Status.SUBMITTED))
        violates(
            "mr_request_number_has_submit_time", lambda: rows.update(number="SOL-X-2026-00001")
        )
        rows.update(status=Status.CANCELLED)  # a discarded draft never had a number

    def test_closure_fields_go_with_the_closed_state(self, make_request: MakeRequest) -> None:
        approved = make_request(Status.APPROVED)
        rows = MaterialRequest.objects.filter(pk=approved.pk)
        violates("mr_request_closure", lambda: rows.update(status=Status.CLOSED))
        violates("mr_request_closure", lambda: rows.update(close_mode="AUTO"))
        rows.update(status=Status.CLOSED, close_mode="MANUAL", closed_at=timezone.now())

    def test_status_and_priority_are_known_values(self, make_request: MakeRequest) -> None:
        rows = MaterialRequest.objects.filter(pk=make_request().pk)
        violates("mr_request_status_valid", lambda: rows.update(status="LOST"))
        violates("mr_request_priority_valid", lambda: rows.update(priority="NOW"))

    def test_numbers_are_unique(self, make_request: MakeRequest) -> None:
        first = make_request(Status.SUBMITTED)
        second = make_request(Status.SUBMITTED)
        violates(
            "number",
            lambda: MaterialRequest.objects.filter(pk=second.pk).update(number=first.number),
        )

    def test_str(self, make_request: MakeRequest) -> None:
        assert str(make_request(Status.SUBMITTED)).startswith("SOL-T-")
        draft = make_request()
        assert str(draft) == f"{draft.project_id} · {draft.get_status_display()}"


class TestLineConstraints:
    @pytest.fixture
    def line(self, make_request: MakeRequest) -> MaterialRequestLine:
        return make_request().lines.get()

    def test_quantities(self, line: MaterialRequestLine) -> None:
        violates("mr_line_requested_positive", line_update(line, requested_qty=0))
        approved = {"decision": "APPROVED", "decided_at": timezone.now()}
        violates(
            "mr_line_approved_within_requested",
            line_update(line, approved_qty=Decimal("10.01"), **approved),
        )
        violates(
            "mr_line_approved_within_requested",
            line_update(line, approved_qty=Decimal("-1"), **approved),
        )
        line_update(line, approved_qty=Decimal("10.00"), **approved)()

    def test_material_or_text(self, line: MaterialRequestLine) -> None:
        violates("mr_line_material_or_text", line_update(line, material=None, free_text=""))
        line_update(line, material=None, free_text="Bornas especiales")()

    def test_decision_fields(self, line: MaterialRequestLine, people: People) -> None:
        now = timezone.now()
        violates("mr_line_decision_fields", line_update(line, decision="APPROVED"))
        violates("mr_line_decision_fields", line_update(line, decided_at=now))
        violates("mr_line_decision_fields", line_update(line, approved_qty=Decimal("1")))
        violates("mr_line_decision_fields", line_update(line, decision="REJECTED"))
        violates(
            "mr_line_decision_valid", line_update(line, decision="MAYBE"), "mr_line_decision_fields"
        )
        line_update(line, decision="REJECTED", decided_at=now, decided_by=people.mm_mad)()

    def test_purchase_needs_a_material(self, line: MaterialRequestLine) -> None:
        violates("mr_line_sourcing_valid", line_update(line, sourcing="STEAL"))
        line_update(line, sourcing="PURCHASE")()
        violates(
            "mr_line_purchase_needs_material",
            line_update(line, material=None, free_text="Algo"),
        )

    def test_closure_fields_all_or_none(self, line: MaterialRequestLine, people: People) -> None:
        now = timezone.now()
        violates("mr_line_closure_fields", line_update(line, closed_short_at=now))
        violates(
            "mr_line_closure_fields",
            line_update(
                line, closed_short_at=now, closed_short_by=people.mm_mad, closed_short_source="LINE"
            ),
        )
        line_update(
            line,
            closed_short_at=now,
            closed_short_by=people.mm_mad,
            closed_short_source="REQUEST",
            close_reason="Obra terminada",
        )()

    def test_line_numbers_are_unique_and_positive(
        self, line: MaterialRequestLine, cable: Material
    ) -> None:
        violates("mr_line_no_positive", line_update(line, line_no=0))
        violates(
            "mr_line_number",
            lambda: MaterialRequestLine.objects.create(
                request=line.request,
                project=line.project,
                line_no=1,
                material=cable,
                unit=cable.unit,
                requested_qty=Decimal("1"),
            ),
        )

    def test_description(self, line: MaterialRequestLine) -> None:
        assert line.description == "CAB-25 · Cable 2,5"
        assert str(line).endswith("#1")
        assert MaterialRequestLine(free_text="Bornas").description == "Bornas"


class TestCompositeKeys:
    """Inconsistent rows written through the ORM, bypassing the services (plan §4.6)."""

    def test_c9_a_line_carries_its_materials_unit(
        self, make_request: MakeRequest, cable: Material
    ) -> None:
        request = make_request(lines=0)
        violates(
            "mr_line_material_unit_fk",
            lambda: MaterialRequestLine.objects.create(
                request=request,
                project=request.project,
                line_no=1,
                material=cable,
                unit=UnitOfMeasure.objects.get(code="UD"),
                requested_qty=Decimal("1"),
            ),
        )

    def test_c9_a_referenced_material_keeps_its_unit(
        self, make_request: MakeRequest, cable: Material, breaker: Material
    ) -> None:
        make_request()
        kg = UnitOfMeasure.objects.get(code="KG")
        violates(
            "mr_line_material_unit_fk",
            lambda: Material.objects.filter(pk=cable.pk).update(unit=kg),
        )
        Material.objects.filter(pk=breaker.pk).update(unit=kg)  # not referenced

    def test_c9_skips_non_catalog_lines(self, make_request: MakeRequest) -> None:
        request = make_request(lines=0)
        MaterialRequestLine.objects.create(
            request=request,
            project=request.project,
            line_no=1,
            free_text="Bornas especiales",
            unit=UnitOfMeasure.objects.get(code="UD"),
            requested_qty=Decimal("4"),
        )

    def test_c3_a_line_has_its_requests_project(
        self, make_request: MakeRequest, org: Org, cable: Material
    ) -> None:
        request = make_request(lines=0)
        violates(
            "mr_line_request_project_fk",
            lambda: MaterialRequestLine.objects.create(
                request=request,
                project=org.sev_works,
                line_no=1,
                material=cable,
                unit=cable.unit,
                requested_qty=Decimal("1"),
            ),
        )

    def test_c3_lines_follow_a_project_change(self, make_request: MakeRequest, org: Org) -> None:
        request = make_request(lines=2)
        MaterialRequest.objects.filter(pk=request.pk).update(project=org.sev_for_mad)
        assert set(request.lines.values_list("project_id", flat=True)) == {org.sev_for_mad.pk}


class TestScope:
    """S5: a request is a document of its procurement branch; own reach follows the project."""

    @pytest.fixture
    def requests(self, make_request: MakeRequest, org: Org, people: People) -> dict[str, object]:
        return {
            "tech_hospital": make_request(user=people.tech_mad),
            "partner_hospital": make_request(user=people.partner),
            "admin_sev_for_mad": make_request(user=people.group_admin, project=org.sev_for_mad),
            "mm_sev_works": make_request(user=people.mm_sev, project=org.sev_works),
        }

    def visible(self, user: User, requests: dict[str, object]) -> set[str]:
        ids = set(
            MaterialRequest.objects.visible_to(
                AccessContext.for_user(user), Capability.REQUEST_VIEW
            ).values_list("pk", flat=True)
        )
        names = {name for name, request in requests.items() if request.pk in ids}  # type: ignore[attr-defined]
        lines = MaterialRequestLine.objects.visible_to(
            AccessContext.for_user(user), Capability.REQUEST_VIEW
        )
        assert set(lines.values_list("request_id", flat=True)) == ids
        return names

    @pytest.mark.parametrize(
        ("person", "expected"),
        [
            (
                "group_admin",
                {"tech_hospital", "partner_hospital", "admin_sev_for_mad", "mm_sev_works"},
            ),
            ("mm_mad", {"tech_hospital", "partner_hospital", "admin_sev_for_mad"}),
            ("wo_mad", {"tech_hospital", "partner_hospital", "admin_sev_for_mad"}),
            ("mm_sev", {"mm_sev_works"}),  # SEV-2 is A-SEV's project but A-MAD buys for it
            ("site_manager", {"tech_hospital", "partner_hospital"}),
            ("tech_mad", {"tech_hospital"}),
            ("partner", {"partner_hospital"}),
            ("revoked_assignment", set()),
            ("inactive_partner", set()),
            ("expired", set()),
            ("nobody", set()),
        ],
    )
    def test_request_view(
        self, people: People, requests: dict[str, object], person: str, expected: set[str]
    ) -> None:
        assert self.visible(getattr(people, person), requests) == expected

    def test_a_technician_of_a_managed_branch_sees_its_own_requests(
        self, make_request: MakeRequest, org: Org, member: Callable[..., User]
    ) -> None:
        """B-MAD's technician asks for the hospital, which A-MAD buys for (plan §3.2)."""
        tech_b = member(Role.TECHNICIAN, branch=org.b_mad)
        own = make_request(user=tech_b)
        other = make_request()
        assert self.visible(tech_b, {"own": own, "other": other}) == {"own"}

    def test_own_requests_stay_visible_when_the_project_is_on_hold(
        self, make_request: MakeRequest, org: Org, people: People
    ) -> None:
        own = make_request(user=people.tech_mad)
        type(org.hospital).objects.filter(pk=org.hospital.pk).update(status="ON_HOLD")
        assert self.visible(people.tech_mad, {"own": own}) == {"own"}

    def test_request_create_reaches_own_drafts_only(
        self, make_request: MakeRequest, people: People
    ) -> None:
        own, other = make_request(user=people.mm_mad), make_request()
        ctx = AccessContext.for_user(people.mm_mad)
        assert ctx.can(Capability.REQUEST_CREATE, own)
        assert not ctx.can(Capability.REQUEST_CREATE, other)
        assert ctx.can(Capability.REQUEST_VIEW, other)
