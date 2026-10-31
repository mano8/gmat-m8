"""``manage seed_demo`` (plan M3.4): the A/B/C organization and demo data, idempotently."""

from io import StringIO
from typing import Any

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.access.context import AccessContext
from apps.access.roles import Role
from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.catalog.models import Material, MaterialCategory
from apps.locations.models import Location
from apps.organizations.models import Branch, Company, JointVentureMember, Membership
from apps.projects import selectors as project_selectors
from apps.projects.models import Project, ProjectAssignment
from apps.suppliers.models import Supplier

pytestmark = pytest.mark.django_db

PASSWORD = "demo-password-for-tests-1"


def seed(*args: str, operator: str = "admin@example.org") -> str:
    out = StringIO()
    call_command("seed_demo", "--operator", operator, *args, stdout=out)
    return out.getvalue()


def census() -> dict[str, int]:
    return {
        model.__name__: model.objects.count()
        for model in (
            Company,
            JointVentureMember,
            Branch,
            Location,
            User,
            Membership,
            Project,
            ProjectAssignment,
            MaterialCategory,
            Material,
            Supplier,
            AuditEvent,
        )
    }


def test_seeds_the_organization_of_the_plan(group_admin: User) -> None:
    out = seed("--password", PASSWORD)
    assert "seed_demo:" in out and "materials: 204 created" in out
    a, b, c = (Company.objects.get(code=code) for code in "ABC")
    assert b.kind == Company.Kind.JOINT_VENTURE
    assert set(
        JointVentureMember.objects.filter(venture=b).values_list("member__code", flat=True)
    ) == {
        "A",
        "C",
    }
    a_mad = Branch.objects.get(company=a, code="MAD")
    b_mad = Branch.objects.get(company=b, code="MAD")
    assert b_mad.managed_by == a_mad
    assert Branch.objects.filter(company=c, code="HQ").exists()  # created with the company
    for branch in (a_mad, Branch.objects.get(company=a, code="SEV"), b_mad):
        assert branch.default_warehouse is not None
        assert branch.default_warehouse.kind == Location.Kind.WAREHOUSE
    hospital = Project.objects.get(code="HOSP-X")
    assert (hospital.branch, hospital.procurement_branch) == (b_mad, a_mad)
    assert Project.objects.get(code="OFI-MAD").status == Project.Status.ON_HOLD
    assert Location.objects.filter(kind=Location.Kind.WORKSITE).count() == 4
    assert Supplier.objects.count() == 5
    assert Material.objects.filter(is_stock_item=True).exists()
    assert Material.objects.count() == 204


def test_one_user_per_role_and_a_mixed_one(group_admin: User) -> None:
    out = seed("--password", PASSWORD)
    roles = set(Membership.objects.exclude(user=group_admin).values_list("role", flat=True))
    assert roles == {
        Role.MATERIAL_MANAGER,
        Role.WAREHOUSE_OPERATOR,
        Role.TECHNICIAN,
        Role.SITE_MANAGER,
        Role.PARTNER_REQUESTER,
    }
    mixed = User.objects.get(email="mixed@demo.example")
    assert sorted(Membership.objects.filter(user=mixed).values_list("role", flat=True)) == [
        Role.MATERIAL_MANAGER,
        Role.TECHNICIAN,
    ]
    partner = User.objects.get(email="partner@demo.example")
    assert partner.employer_company == Company.objects.get(code="C")
    assert partner.check_password(PASSWORD)
    assert ProjectAssignment.objects.count() == 2
    assert "mm-mad@demo.example" in out and PASSWORD in out


def test_the_demo_people_see_what_the_plan_says_they_see(group_admin: User) -> None:
    seed("--password", PASSWORD)

    def visible(email: str) -> set[str]:
        ctx = AccessContext.for_user(User.objects.get(email=email))
        return set(project_selectors.projects(ctx).values_list("code", flat=True))

    assert visible("admin@example.org") == {
        "HOSP-X",
        "COL-MAD",
        "NAVE-SEV",
        "OFI-MAD",
    }
    assert visible("mm-mad@demo.example") == {"HOSP-X", "COL-MAD", "OFI-MAD"}  # B-MAD is managed
    assert visible("mm-sev@demo.example") == {"NAVE-SEV"}
    assert visible("tech-mad@demo.example") == {"HOSP-X", "COL-MAD"}  # active projects only
    assert visible("site-manager@demo.example") == {"HOSP-X"}
    assert visible("partner@demo.example") == {"HOSP-X"}


def test_running_it_again_changes_nothing(group_admin: User) -> None:
    seed("--password", PASSWORD)
    before = census()
    out = seed("--password", "another-password-ignored-1")
    assert census() == before
    assert "0 created" in out and " created, 0 existing" not in out
    assert "Demo users created now" not in out  # nothing new, so no passwords
    assert User.objects.get(email="tech-mad@demo.example").check_password(PASSWORD)


def test_it_completes_a_partial_seed(group_admin: User) -> None:
    seed("--password", PASSWORD)
    Material.objects.filter(reference__startswith="CAB-H07VK-1,5").delete()
    out = seed()
    assert "materials: 5 created, 199 existing" in out
    assert Material.objects.count() == 204


def test_a_random_password_is_generated_and_printed_once(group_admin: User) -> None:
    out = seed()
    line = next(line for line in out.splitlines() if "password shown once" in line)
    password = line.split("password shown once: ")[1].rstrip("):")
    assert len(password) >= 12
    assert User.objects.get(email="wo-mad@demo.example").check_password(password)


@pytest.mark.parametrize("module", ["config.settings.prod", "config.settings.build", ""])
def test_it_refuses_production_settings_before_touching_anything(
    group_admin: User, monkeypatch: pytest.MonkeyPatch, module: str
) -> None:
    before = census()
    if module:
        monkeypatch.setenv("DJANGO_SETTINGS_MODULE", module)
    else:
        monkeypatch.delenv("DJANGO_SETTINGS_MODULE")
    with pytest.raises(CommandError, match="refuses to run with production settings"):
        seed()
    assert census() == before


@pytest.mark.parametrize("password", ["short", "12345678901234567890"])
def test_a_weak_password_is_refused(group_admin: User, password: str) -> None:
    with pytest.raises(CommandError):
        seed("--password", password)
    assert not Company.objects.exists()


def test_only_a_group_administrator_may_seed(org: Any, people: Any) -> None:
    for who in (people.mm_mad, people.tech_mad, people.nobody):
        with pytest.raises(CommandError, match=r"does not hold org\.manage|active user"):
            seed(operator=who.email)
    assert not Material.objects.exists()


def test_the_audit_trail_names_the_operator(group_admin: User) -> None:
    seed("--password", PASSWORD)
    events = AuditEvent.objects.filter(action="material.created")
    assert events.count() == 204
    assert set(events.values_list("via", flat=True)) == {"cli:seed_demo"}
    assert set(events.values_list("actor_label", flat=True)) == {group_admin.email}
