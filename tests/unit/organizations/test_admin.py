"""Admin for organizations, locations and projects (plan §5 "Django Admin", §8.2).

Group administrators reach the admin with session MFA; every model is read-only there,
master data included (ADR-0030: it changes in the app's screens); staff accounts are out of
a group administrator's reach; nothing is deleted.
"""

from collections.abc import Callable
from typing import Any

import pytest
from django.contrib import admin
from django.test import Client
from django.urls import reverse

from apps.access.generations import org_generation
from apps.access.models import AccessState
from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.core.admin import ReadOnlyModelAdmin
from apps.locations.models import Location
from apps.organizations.models import Branch, Company, JointVentureMember, Membership
from apps.projects.models import Project, ProjectAssignment
from tests.support import enroll_totp, mfa_login

pytestmark = pytest.mark.django_db


def url(model: type[Any], view: str, *args: Any) -> str:
    return reverse(f"admin:{model._meta.app_label}_{model._meta.model_name}_{view}", args=args)


@pytest.fixture
def admin_client(client: Client, group_admin: User, frozen_totp: int) -> Client:
    """A group administrator whose session completed MFA."""
    mfa_login(client, group_admin.email, enroll_totp(group_admin))
    return client


@pytest.fixture
def operator_client(make_user: Callable[..., User], frozen_totp: int) -> Client:
    """A superuser without any membership: a platform operator."""
    operator = make_user("ops@example.org", is_staff=True, is_superuser=True)
    client = Client()
    mfa_login(client, operator.email, enroll_totp(operator))
    return client


class TestAdmission:
    def test_a_group_admin_with_session_mfa_is_admitted(self, admin_client: Client) -> None:
        assert admin_client.get(reverse("admin:index")).status_code == 200
        assert admin_client.get(url(Company, "changelist")).status_code == 200

    def test_a_group_admin_without_session_mfa_is_not(
        self, client: Client, group_admin: User
    ) -> None:
        client.force_login(group_admin)
        response = client.get(reverse("admin:index"))
        assert response.status_code == 302
        assert reverse("admin:login") not in response["Location"]

    def test_other_roles_are_refused(self, client: Client, make_user: Callable[..., User]) -> None:
        client.force_login(make_user("staff@example.org", is_staff=True))
        assert client.get(reverse("admin:login")).status_code == 403

    def test_back_from_the_allauth_login_a_group_admin_goes_on(self, admin_client: Client) -> None:
        response = admin_client.get(reverse("admin:login") + "?next=/admin/organizations/")
        assert response["Location"] == "/admin/organizations/"


def _master_data(company: Company, branch: Branch) -> dict[type[Any], Any]:
    """One record of each model the admin used to edit (ADR-0030: now read-only)."""
    from apps.catalog.models import Material, MaterialCategory, UnitOfMeasure
    from apps.suppliers.models import Supplier

    venture = Company.objects.create(code="B", legal_name="UTE B", kind=Company.Kind.JOINT_VENTURE)
    category = MaterialCategory.objects.create(name="Electricidad")
    unit = UnitOfMeasure.objects.order_by("code")[0]
    return {
        Company: company,
        Branch: branch,
        JointVentureMember: JointVentureMember.objects.create(
            venture=venture, member=company, share_pct=60
        ),
        Location: Location.objects.create(
            kind=Location.Kind.WAREHOUSE, branch=branch, name="Almacén"
        ),
        MaterialCategory: category,
        Material: Material.objects.create(
            reference="CAB-1", name="Cable", unit=unit, category=category
        ),
        UnitOfMeasure: unit,
        Supplier: Supplier.objects.create(name="Proveedor"),
    }


def _snapshot(obj: Any) -> dict[str, Any]:
    fresh = type(obj).objects.get(pk=obj.pk)
    return {field.attname: getattr(fresh, field.attname) for field in fresh._meta.concrete_fields}


class TestMasterDataIsReadOnly:
    """Master data is changed only in the app's screens (ADR-0030, M10.5)."""

    def test_a_group_admin_sees_it_and_cannot_save(
        self, admin_client: Client, company: Company, branch: Branch
    ) -> None:
        for model, obj in _master_data(company, branch).items():
            assert admin_client.get(url(model, "changelist")).status_code == 200, model
            change = url(model, "change", obj.pk)
            page = admin_client.get(change)
            assert page.status_code == 200, model
            assert page.context["has_change_permission"] is False, model
            assert b'name="_save"' not in page.content, model
            before = _snapshot(obj)
            assert admin_client.post(change, {"name": "Hijacked"}).status_code == 403, model
            assert _snapshot(obj) == before, model
            assert admin_client.get(url(model, "add")).status_code == 403, model
            assert admin_client.post(url(model, "add"), {"name": "New"}).status_code == 403
            assert admin_client.post(url(model, "delete", obj.pk)).status_code == 403, model

    def test_no_bulk_action_is_offered_or_run(self, admin_client: Client, company: Company) -> None:
        changelist = url(Company, "changelist")
        assert admin_client.get(changelist).context["action_form"] is None
        response = admin_client.post(
            changelist, {"action": "delete_selected", "_selected_action": [str(company.pk)]}
        )
        assert response.status_code in {200, 302}
        assert Company.objects.filter(pk=company.pk, is_active=True).exists()

    def test_nothing_changes_the_generation_or_the_audit(
        self, admin_client: Client, company: Company
    ) -> None:
        before = (org_generation(), AuditEvent.objects.count())
        admin_client.post(
            url(Company, "change", company.pk),
            {"legal_name": "A S.A.", "kind": "STANDARD", "is_active": ""},
        )
        admin_client.post(url(Company, "add"), {"code": "C", "legal_name": "C", "kind": "STANDARD"})
        assert (org_generation(), AuditEvent.objects.count()) == before
        assert not Company.objects.filter(code="C").exists()

    def test_a_platform_operator_only_views_it(
        self, operator_client: Client, company: Company
    ) -> None:
        assert operator_client.get(url(Company, "changelist")).status_code == 200
        assert operator_client.get(url(Company, "change", company.pk)).status_code == 200
        assert operator_client.get(url(Company, "add")).status_code == 403
        response = operator_client.post(
            url(Company, "change", company.pk),
            {"legal_name": "Hijacked", "kind": "STANDARD", "is_active": "on"},
        )
        assert response.status_code == 403
        assert Company.objects.get(pk=company.pk).legal_name == "Company A"


class TestReadOnlyAndStaff:
    @pytest.mark.parametrize(
        "model",
        [
            Membership,
            Project,
            ProjectAssignment,
            AccessState,
            User,
            Company,
            Branch,
            JointVentureMember,
            Location,
        ],
    )
    def test_every_model_is_read_only(self, model: type[Any]) -> None:
        assert type(admin.site._registry[model]).__mro__[1] is ReadOnlyModelAdmin

    def test_a_group_admin_cannot_edit_a_staff_account(
        self, admin_client: Client, make_user: Callable[..., User]
    ) -> None:
        staff = make_user("ops@example.org", is_staff=True, is_superuser=True)
        change = url(User, "change", staff.pk)
        assert admin_client.get(change).status_code == 200  # view only
        response = admin_client.post(
            change, {"email": "ops@example.org", "is_staff": "", "is_superuser": ""}
        )
        assert response.status_code == 403
        staff.refresh_from_db()
        assert (staff.is_staff, staff.is_superuser) == (True, True)
        assert admin_client.get(url(User, "add")).status_code == 403

    def test_nothing_is_deleted(self, admin_client: Client, company: Company) -> None:
        assert admin_client.post(url(Company, "delete", company.pk)).status_code == 403
        assert Company.objects.filter(pk=company.pk).exists()
