"""Admin for organizations, locations and projects (plan §5 "Django Admin", §8.2).

Group administrators reach the admin with session MFA; master data saves through the
services (generation bumped, audit written); access models are read-only; staff
accounts are out of a group administrator's reach; nothing is deleted.
"""

from collections.abc import Callable, Collection
from typing import Any
from uuid import UUID

import pytest
from django.contrib import admin
from django.test import Client
from django.urls import reverse

from apps.access.generations import org_generation
from apps.access.models import AccessState
from apps.access.principals import SessionPrincipal
from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.core.admin import ReadOnlyModelAdmin
from apps.locations.models import Location
from apps.organizations.admin import MasterDataAdmin
from apps.organizations.deactivation import OpenDocument, register_open_documents
from apps.organizations.models import Branch, Company, JointVentureMember, Membership
from apps.organizations.services import create_company
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


class TestMasterData:
    def test_create_a_company_through_the_service(self, admin_client: Client) -> None:
        before = org_generation()
        response = admin_client.post(
            url(Company, "add"),
            {"code": "C", "legal_name": "Partner C", "tax_id": "", "kind": "STANDARD"},
        )
        assert response.status_code == 302, response.content
        company = Company.objects.get(code="C")
        assert company.tax_id is None
        assert Branch.objects.filter(company=company, code="HQ").exists()
        assert AuditEvent.objects.filter(action="company.created", object_id=company.pk).exists()
        assert org_generation() > before

    def test_change_a_company_through_the_service(
        self, admin_client: Client, company: Company
    ) -> None:
        response = admin_client.post(
            url(Company, "change", company.pk),
            {"legal_name": "A S.A.", "tax_id": "", "kind": "STANDARD", "is_active": "on"},
        )
        assert response.status_code == 302, response.content
        assert Company.objects.get(pk=company.pk).legal_name == "A S.A."
        event = AuditEvent.objects.get(action="company.updated")
        assert event.data == {"legal_name": {"from": "Company A", "to": "A S.A."}}

    def test_code_is_read_only_after_creation(self, admin_client: Client, company: Company) -> None:
        admin_client.post(
            url(Company, "change", company.pk),
            {"code": "Z", "legal_name": "Company A", "kind": "STANDARD", "is_active": "on"},
        )
        assert Company.objects.get(pk=company.pk).code == "A"

    def test_a_refusal_under_lock_is_shown(self, admin_client: Client, company: Company) -> None:
        def provider(branch_ids: Collection[UUID]) -> list[OpenDocument]:
            return [OpenDocument("purchase_order", "PED-A-2026-00001", UUID(int=1))]

        unregister = register_open_documents(provider)
        try:
            change = url(Company, "change", company.pk)
            response = admin_client.post(
                change, {"legal_name": "Company A", "tax_id": "", "kind": "STANDARD"}, follow=True
            )
        finally:
            unregister()
        assert response.redirect_chain[0] == (change, 302)
        assert "PED-A-2026-00001" in response.content.decode()
        assert Company.objects.get(pk=company.pk).is_active is True

    def test_form_validation_runs_before_the_service(
        self, admin_client: Client, company: Company, branch: Branch
    ) -> None:
        hq = Branch.objects.get(company=company, code="HQ")
        admin_client.post(
            url(Branch, "change", branch.pk),
            {"name": "Madrid", "managed_by": str(hq.pk), "is_active": "on"},
        )
        response = admin_client.post(
            url(Branch, "change", hq.pk),
            {"name": "HQ", "managed_by": str(branch.pk), "is_active": "on"},
        )
        assert response.status_code == 200  # the form is shown again with the error
        assert Branch.objects.get(pk=hq.pk).managed_by_id is None

    def test_branch_create_and_change(self, admin_client: Client, company: Company) -> None:
        response = admin_client.post(
            url(Branch, "add"),
            {"company": str(company.pk), "code": "SEV", "name": "Sevilla", "city": "Sevilla"},
        )
        assert response.status_code == 302, response.content
        branch = Branch.objects.get(company=company, code="SEV")
        warehouse = Location.objects.create(
            kind=Location.Kind.WAREHOUSE, branch=branch, name="Almacén SEV"
        )
        form = admin_client.get(url(Branch, "change", branch.pk)).context["adminform"].form
        assert list(form.fields["default_warehouse"].queryset) == [warehouse]
        assert branch not in form.fields["managed_by"].queryset
        before = org_generation()
        response = admin_client.post(
            url(Branch, "change", branch.pk),
            {"name": "Sevilla", "default_warehouse": str(warehouse.pk)},
        )
        assert response.status_code == 302, response.content
        branch.refresh_from_db()
        assert (branch.default_warehouse_id, branch.is_active) == (warehouse.pk, False)
        assert org_generation() == before + 1  # deactivated in the same save

    def test_joint_venture_members(
        self, admin_client: Client, admin_principal: SessionPrincipal, company: Company
    ) -> None:
        venture = create_company(
            admin_principal, code="B", legal_name="UTE B", kind=Company.Kind.JOINT_VENTURE
        )
        form = admin_client.get(url(JointVentureMember, "add")).context["adminform"].form
        assert list(form.fields["venture"].queryset) == [venture]
        response = admin_client.post(
            url(JointVentureMember, "add"),
            {"venture": str(venture.pk), "member": str(company.pk), "share_pct": "60"},
        )
        assert response.status_code == 302, response.content
        member = JointVentureMember.objects.get()
        response = admin_client.post(
            url(JointVentureMember, "change", member.pk), {"share_pct": "40"}
        )
        assert response.status_code == 302, response.content
        member.refresh_from_db()
        assert str(member.share_pct) == "40.00"

    def test_warehouses(self, admin_client: Client, branch: Branch) -> None:
        response = admin_client.post(
            url(Location, "add"), {"branch": str(branch.pk), "name": "Almacén", "address": ""}
        )
        assert response.status_code == 302, response.content
        warehouse = Location.objects.get(branch=branch)
        assert warehouse.kind == Location.Kind.WAREHOUSE
        response = admin_client.post(
            url(Location, "change", warehouse.pk), {"name": "Central", "address": ""}
        )
        assert response.status_code == 302, response.content
        warehouse.refresh_from_db()
        assert (warehouse.name, warehouse.is_active) == ("Central", False)

    def test_a_platform_operator_only_views_master_data(
        self, operator_client: Client, company: Company
    ) -> None:
        assert operator_client.get(url(Company, "changelist")).status_code == 200
        assert operator_client.get(url(Company, "change", company.pk)).status_code == 200
        assert operator_client.get(url(Company, "add")).status_code == 403
        operator_client.post(
            url(Company, "change", company.pk),
            {"legal_name": "Hijacked", "kind": "STANDARD", "is_active": "on"},
        )
        assert Company.objects.get(pk=company.pk).legal_name == "Company A"


class TestReadOnlyAndStaff:
    @pytest.mark.parametrize("model", [Membership, Project, ProjectAssignment, AccessState, User])
    def test_access_models_are_read_only(self, model: type[Any]) -> None:
        assert isinstance(admin.site._registry[model], ReadOnlyModelAdmin)

    @pytest.mark.parametrize("model", [Company, Branch, JointVentureMember, Location])
    def test_master_data_is_service_backed(self, model: type[Any]) -> None:
        assert isinstance(admin.site._registry[model], MasterDataAdmin)

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
