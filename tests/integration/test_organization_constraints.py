"""Database constraints of the organization models (plan §3.3), whatever path writes.

Rows here are written with the ORM directly, past ``full_clean`` and the services, so
each test shows the database itself refusing the row.
"""

from collections.abc import Callable
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from apps.access.roles import Role
from apps.accounts.models import User
from apps.locations.models import Location
from apps.organizations.models import Branch, Company, JointVentureMember, Membership
from apps.projects.models import Project, ProjectAssignment

pytestmark = [pytest.mark.integration, pytest.mark.django_db]

NOW = timezone.now()


def refused(match: str = "") -> Any:
    return pytest.raises(IntegrityError, match=match or None)


@pytest.fixture
def a() -> Company:
    return Company.objects.create(code="A", legal_name="A")


@pytest.fixture
def venture() -> Company:
    return Company.objects.create(code="B", legal_name="B", kind=Company.Kind.JOINT_VENTURE)


@pytest.fixture
def mad(a: Company) -> Branch:
    return Branch.objects.create(company=a, code="MAD", name="Madrid")


@pytest.fixture
def sev(a: Company) -> Branch:
    return Branch.objects.create(company=a, code="SEV", name="Sevilla")


@pytest.fixture
def user(make_user: Callable[..., User]) -> User:
    return make_user()


@pytest.fixture
def project(mad: Branch) -> Project:
    return Project.objects.create(code="P1", name="P1", branch=mad, procurement_branch=mad)


class TestCompany:
    @pytest.mark.parametrize(
        "fields",
        [
            {"code": "a-lower"},
            {"code": "-A"},
            {"code": "A:B"},
            {"tax_id": ""},
            {"kind": "OTHER"},
        ],
    )
    def test_refused(self, fields: dict[str, Any]) -> None:
        with refused(), transaction.atomic():
            Company.objects.create(**{"code": "OK", "legal_name": "X", **fields})

    def test_code_and_tax_id_are_unique(self, a: Company) -> None:
        Company.objects.filter(pk=a.pk).update(tax_id="B1")
        with refused(), transaction.atomic():
            Company.objects.create(code="A", legal_name="Again")
        with refused(), transaction.atomic():
            Company.objects.create(code="Z", legal_name="Z", tax_id="B1")
        Company.objects.create(code="Y", legal_name="Y")  # NULL tax ids do not collide

    def test_clean_turns_a_blank_tax_id_into_null(self) -> None:
        company = Company(code="C", legal_name="C", tax_id="  ")
        company.full_clean()
        assert company.tax_id is None


class TestJointVentureMember:
    def test_the_venture_must_be_a_joint_venture(self, a: Company, venture: Company) -> None:
        with refused("organizations_jv_venture_kind_fk"), transaction.atomic():
            JointVentureMember.objects.create(venture=a, member=venture)

    def test_a_venture_with_members_keeps_its_kind(self, a: Company, venture: Company) -> None:
        JointVentureMember.objects.create(venture=venture, member=a)
        with refused("organizations_jv_venture_kind_fk"), transaction.atomic():
            Company.objects.filter(pk=venture.pk).update(kind=Company.Kind.STANDARD)

    @pytest.mark.parametrize(
        "fields",
        [{"self": True}, {"share_pct": Decimal(0)}, {"share_pct": Decimal("100.01")}],
    )
    def test_refused(self, a: Company, venture: Company, fields: dict[str, Any]) -> None:
        member = venture if fields.pop("self", False) else a
        with refused(), transaction.atomic():
            JointVentureMember.objects.create(venture=venture, member=member, **fields)

    def test_one_row_per_pair(self, a: Company, venture: Company) -> None:
        JointVentureMember.objects.create(venture=venture, member=a, share_pct=Decimal(100))
        with refused(), transaction.atomic():
            JointVentureMember.objects.create(venture=venture, member=a)


class TestBranch:
    def test_code_is_unique_per_company(self, mad: Branch, venture: Company) -> None:
        with refused(), transaction.atomic():
            Branch.objects.create(company=mad.company, code="MAD", name="Again")
        Branch.objects.create(company=venture, code="MAD", name="B Madrid")

    def test_cannot_manage_itself(self, mad: Branch) -> None:
        with refused("organizations_branch_not_self"), transaction.atomic():
            Branch.objects.filter(pk=mad.pk).update(managed_by=mad)

    def test_one_level_only(self, mad: Branch, sev: Branch, venture: Company) -> None:
        b_mad = Branch.objects.create(company=venture, code="MAD", name="B", managed_by=mad)
        # A managed branch cannot manage others.
        with refused("cannot manage other branches"), transaction.atomic():
            Branch.objects.filter(pk=sev.pk).update(managed_by=b_mad)
        with refused("cannot manage other branches"), transaction.atomic():
            Branch.objects.create(company=venture, code="X", name="X", managed_by=b_mad)
        # A managing branch cannot be managed.
        with refused("that manages others cannot be managed"), transaction.atomic():
            Branch.objects.filter(pk=mad.pk).update(managed_by=sev)
        Branch.objects.filter(pk=sev.pk).update(managed_by=mad)  # a second managed branch

    def test_the_default_warehouse_belongs_to_the_branch(self, mad: Branch, sev: Branch) -> None:
        foreign = Location.objects.create(kind=Location.Kind.WAREHOUSE, branch=sev, name="SEV")
        with refused("organizations_branch_default_warehouse_fk"), transaction.atomic():
            Branch.objects.filter(pk=mad.pk).update(default_warehouse=foreign)
        own = Location.objects.create(kind=Location.Kind.WAREHOUSE, branch=mad, name="MAD")
        Branch.objects.filter(pk=mad.pk).update(default_warehouse=own)
        with refused("organizations_branch_default_warehouse_fk"), transaction.atomic():
            Location.objects.filter(pk=own.pk).update(branch=sev)


SHAPES: dict[Role, list[dict[str, bool]]] = {
    Role.GROUP_ADMIN: [{"branch": True}, {"company": True}],
    Role.MATERIAL_MANAGER: [{}, {"branch": True, "company": True}, {"company": True}],
    Role.WAREHOUSE_OPERATOR: [{}, {"company": True}],
    Role.TECHNICIAN: [{}, {"company": True}],
    Role.SITE_MANAGER: [{}, {"company": True}],
    Role.PARTNER_REQUESTER: [{}, {"branch": True}, {"branch": True, "company": True}],
}
VALID: dict[Role, dict[str, bool]] = {
    Role.GROUP_ADMIN: {},
    Role.MATERIAL_MANAGER: {"branch": True},
    Role.WAREHOUSE_OPERATOR: {"branch": True},
    Role.TECHNICIAN: {"branch": True},
    Role.SITE_MANAGER: {"branch": True},
    Role.PARTNER_REQUESTER: {"company": True},
}


class TestMembership:
    def make(
        self, user: User, mad: Branch, role: str, shape: dict[str, bool], **extra: Any
    ) -> Membership:
        return Membership.objects.create(
            user=user,
            role=role,
            branch=mad if shape.get("branch") else None,
            company=mad.company if shape.get("company") else None,
            valid_from=extra.pop("valid_from", NOW),
            **extra,
        )

    @pytest.mark.parametrize(
        ("role", "shape"),
        [(role, shape) for role, shapes in SHAPES.items() for shape in shapes],
        ids=lambda value: str(value),
    )
    def test_wrong_shape_is_refused(
        self, user: User, mad: Branch, role: Role, shape: dict[str, bool]
    ) -> None:
        constraint = f"organizations_membership_shape_{role.value.lower()}"
        with refused(constraint), transaction.atomic():
            self.make(user, mad, role, shape)

    @pytest.mark.parametrize("role", list(Role))
    def test_right_shape_is_accepted(self, user: User, mad: Branch, role: Role) -> None:
        self.make(user, mad, role, VALID[role])

    def test_unknown_role_is_refused(self, user: User, mad: Branch) -> None:
        with refused("organizations_membership_role_valid"), transaction.atomic():
            self.make(user, mad, "OWNER", {})

    @pytest.mark.parametrize("until", [NOW, NOW - timedelta(seconds=1)])
    def test_validity_is_a_window(self, user: User, mad: Branch, until: Any) -> None:
        with refused("organizations_membership_validity"), transaction.atomic():
            self.make(user, mad, Role.GROUP_ADMIN, {}, valid_until=until)

    def test_revoked_by_needs_a_time(self, user: User, mad: Branch) -> None:
        with refused("organizations_membership_revoked_by_needs_time"), transaction.atomic():
            self.make(user, mad, Role.GROUP_ADMIN, {}, revoked_by=user)

    def test_never_deleted(self, user: User, mad: Branch) -> None:
        membership = self.make(user, mad, Role.GROUP_ADMIN, {})
        with refused("revoked, never deleted"), transaction.atomic():
            Membership.objects.filter(pk=membership.pk).delete()
        with refused(), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("DELETE FROM organizations_membership")


class TestLocation:
    def test_project_if_and_only_if_worksite(self, mad: Branch, project: Project) -> None:
        with refused("locations_location_project_iff_worksite"), transaction.atomic():
            Location.objects.create(kind=Location.Kind.WORKSITE, branch=mad, name="W")
        with refused("locations_location_project_iff_worksite"), transaction.atomic():
            Location.objects.create(
                kind=Location.Kind.WAREHOUSE, branch=mad, project=project, name="W"
            )
        with refused("locations_location_kind_valid"), transaction.atomic():
            Location.objects.create(kind="VEHICLE", branch=mad, name="Van")

    def test_one_worksite_per_project(self, mad: Branch, project: Project) -> None:
        Location.objects.create(kind=Location.Kind.WORKSITE, branch=mad, project=project, name="W")
        with refused(), transaction.atomic():
            Location.objects.create(
                kind=Location.Kind.WORKSITE, branch=mad, project=project, name="W2"
            )


class TestProject:
    @pytest.mark.parametrize(
        "fields",
        [
            {"code": "p1"},
            {"status": "DONE"},
            {"start_date": date(2026, 2, 1), "end_date": date(2026, 1, 31)},
        ],
    )
    def test_refused(self, mad: Branch, fields: dict[str, Any]) -> None:
        with refused(), transaction.atomic():
            Project.objects.create(
                **{"code": "P2", "name": "P", "branch": mad, "procurement_branch": mad, **fields}
            )

    def test_one_open_assignment_per_kind(self, project: Project, user: User) -> None:
        def assign(kind: str, **extra: Any) -> ProjectAssignment:
            return ProjectAssignment.objects.create(
                project=project, user=user, kind=kind, valid_from=NOW, **extra
            )

        first = assign(ProjectAssignment.Kind.MANAGER)
        with refused("projects_assignment_one_open"), transaction.atomic():
            assign(ProjectAssignment.Kind.MANAGER)
        assign(ProjectAssignment.Kind.PARTNER)
        ProjectAssignment.objects.filter(pk=first.pk).update(revoked_at=NOW)
        assign(ProjectAssignment.Kind.MANAGER)

    @pytest.mark.parametrize(
        "fields",
        [
            {"kind": "OWNER"},
            {"valid_until": NOW},
            {"revoked_by_self": True},
        ],
    )
    def test_assignment_refused(self, project: Project, user: User, fields: dict[str, Any]) -> None:
        if fields.pop("revoked_by_self", False):
            fields["revoked_by"] = user
        with refused(), transaction.atomic():
            ProjectAssignment.objects.create(
                **{"project": project, "user": user, "kind": "MANAGER", "valid_from": NOW, **fields}
            )

    def test_assignments_are_never_deleted(self, project: Project, user: User) -> None:
        assignment = ProjectAssignment.objects.create(
            project=project, user=user, kind="PARTNER", valid_from=NOW
        )
        with refused("revoked, never deleted"), transaction.atomic():
            ProjectAssignment.objects.filter(pk=assignment.pk).delete()
