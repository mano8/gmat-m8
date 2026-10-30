"""Project and assignment services (plan §3.3, §3.6)."""

from collections.abc import Callable
from datetime import date, timedelta

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.access.authorization import AccessDenied
from apps.access.generations import org_generation
from apps.access.principals import SessionPrincipal
from apps.access.roles import Role
from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.core.idempotency import issue_key
from apps.locations.models import Location
from apps.organizations.models import Branch, Company
from apps.organizations.services import create_branch, create_company, update_branch, update_company
from apps.projects import services
from apps.projects.models import Project, ProjectAssignment
from tests.support import authz_generation, grant, principal_for

pytestmark = pytest.mark.django_db


@pytest.fixture
def b_mad(admin_principal: SessionPrincipal, branch: Branch) -> Branch:
    """Branch ``B-MAD`` of joint venture B, managed by ``A-MAD`` (plan §3.2)."""
    venture = create_company(
        admin_principal, code="B", legal_name="UTE B", kind=Company.Kind.JOINT_VENTURE
    )
    b_mad = create_branch(admin_principal, company_id=venture.pk, code="MAD", name="B Madrid")
    return update_branch(admin_principal, b_mad.pk, managed_by_id=branch.pk)


@pytest.fixture
def project(admin_principal: SessionPrincipal, branch: Branch) -> Project:
    return services.create_project(admin_principal, code="P-1", name="Obra 1", branch_id=branch.pk)


class TestProjects:
    def test_create_adds_the_worksite_and_bumps(
        self, admin_principal: SessionPrincipal, branch: Branch
    ) -> None:
        before = org_generation()
        key = issue_key(admin_principal.user_id, "project.create")
        project = services.create_project(
            admin_principal,
            code="HOSP-X",
            name="Hospital X",
            branch_id=branch.pk,
            address="Av. 1",
            start_date=date(2026, 11, 1),
            idempotency_key=key,
        )
        worksite = Location.objects.get(project=project)
        assert (worksite.kind, worksite.branch_id, worksite.name, worksite.address) == (
            Location.Kind.WORKSITE,
            branch.pk,
            "Hospital X",
            "Av. 1",
        )
        assert project.procurement_branch_id == branch.pk  # an unmanaged branch buys itself
        assert project.status == Project.Status.ACTIVE
        assert org_generation() > before
        assert {"project.created", "location.created"} <= set(
            AuditEvent.objects.values_list("action", flat=True)
        )
        assert str(project) == "HOSP-X · Hospital X"

    def test_a_managed_branch_buys_through_its_manager(
        self, admin_principal: SessionPrincipal, branch: Branch, b_mad: Branch
    ) -> None:
        project = services.create_project(
            admin_principal, code="HOSP-X", name="Hospital X", branch_id=b_mad.pk
        )
        assert (project.branch_id, project.procurement_branch_id) == (b_mad.pk, branch.pk)

    def test_explicit_procurement_branch(
        self, admin_principal: SessionPrincipal, branch: Branch, b_mad: Branch
    ) -> None:
        project = services.create_project(
            admin_principal,
            code="P-2",
            name="P2",
            branch_id=b_mad.pk,
            procurement_branch_id=b_mad.pk,
        )
        assert project.procurement_branch_id == b_mad.pk

    def test_not_on_an_inactive_branch_or_company(
        self, admin_principal: SessionPrincipal, company: Company, branch: Branch, b_mad: Branch
    ) -> None:
        update_branch(admin_principal, branch.pk, is_active=False)
        with pytest.raises(ValidationError) as excinfo:
            services.create_project(admin_principal, code="P", name="P", branch_id=branch.pk)
        assert "branch" in excinfo.value.message_dict
        with pytest.raises(ValidationError) as excinfo:
            services.create_project(admin_principal, code="P", name="P", branch_id=b_mad.pk)
        assert "procurement_branch" in excinfo.value.message_dict
        update_branch(admin_principal, branch.pk, is_active=True)
        update_company(admin_principal, company.pk, is_active=False)
        with pytest.raises(ValidationError):
            services.create_project(admin_principal, code="P", name="P", branch_id=branch.pk)

    def test_create_validates(self, admin_principal: SessionPrincipal, branch: Branch) -> None:
        with pytest.raises(ValidationError):
            services.create_project(
                admin_principal,
                code="P",
                name="P",
                branch_id=branch.pk,
                start_date=date(2026, 2, 1),
                end_date=date(2026, 1, 1),
            )

    @pytest.mark.parametrize("field", ["status", "branch_id", "procurement_branch_id"])
    def test_scope_defining_changes_bump(
        self,
        admin_principal: SessionPrincipal,
        project: Project,
        b_mad: Branch,
        field: str,
    ) -> None:
        value = Project.Status.ON_HOLD if field == "status" else b_mad.pk
        before = org_generation()
        updated = services.update_project(admin_principal, project.pk, **{field: value})
        assert getattr(updated, field) == value
        assert org_generation() == before + 1

    def test_the_worksite_follows_the_branch(
        self, admin_principal: SessionPrincipal, project: Project, b_mad: Branch
    ) -> None:
        services.update_project(admin_principal, project.pk, branch_id=b_mad.pk)
        assert Location.objects.get(project=project).branch_id == b_mad.pk

    def test_other_changes_do_not_bump(
        self, admin_principal: SessionPrincipal, project: Project
    ) -> None:
        before = org_generation()
        services.update_project(admin_principal, project.pk, name="Obra uno", client_name="Cliente")
        assert org_generation() == before
        services.update_project(admin_principal, project.pk, name="Obra uno")
        assert AuditEvent.objects.filter(action="project.updated").count() == 1

    def test_changes_go_only_to_active_branches(
        self, admin_principal: SessionPrincipal, project: Project, b_mad: Branch
    ) -> None:
        update_branch(admin_principal, b_mad.pk, is_active=False)
        for field in ("branch_id", "procurement_branch_id"):
            with pytest.raises(ValidationError):
                services.update_project(admin_principal, project.pk, **{field: b_mad.pk})

    def test_code_is_fixed(self, admin_principal: SessionPrincipal, project: Project) -> None:
        with pytest.raises(ValueError, match="code"):
            services.update_project(admin_principal, project.pk, code="P-9")

    def test_update_validates(self, admin_principal: SessionPrincipal, project: Project) -> None:
        with pytest.raises(ValidationError):
            services.update_project(admin_principal, project.pk, status="DONE")

    def test_needs_project_manage(
        self, make_user: Callable[..., User], branch: Branch, project: Project
    ) -> None:
        technician = make_user("tech@example.org")
        grant(technician, Role.TECHNICIAN, branch=branch)
        principal = principal_for(technician)
        with pytest.raises(AccessDenied):
            services.create_project(principal, code="P-9", name="P", branch_id=branch.pk)
        with pytest.raises(AccessDenied):
            services.update_project(principal, project.pk, name="X")


class TestAssignments:
    @pytest.fixture
    def site_manager(self, make_user: Callable[..., User]) -> User:
        return make_user("site@example.org")

    def test_assign_bumps_the_users_generation(
        self, admin_principal: SessionPrincipal, project: Project, site_manager: User
    ) -> None:
        before = authz_generation(site_manager)
        key = issue_key(admin_principal.user_id, "project_assignment.grant")
        assignment = services.assign_to_project(
            admin_principal,
            project_id=project.pk,
            user_id=site_manager.pk,
            kind=ProjectAssignment.Kind.MANAGER,
            idempotency_key=key,
        )
        assert authz_generation(site_manager) == before + 1
        assert assignment.valid_from is not None
        assert AuditEvent.objects.get(action="project_assignment.granted").branch_id == (
            project.branch_id
        )
        assert str(assignment).endswith("MANAGER")

    def test_one_open_assignment_per_kind(
        self, admin_principal: SessionPrincipal, project: Project, site_manager: User
    ) -> None:
        def assign(kind: str) -> ProjectAssignment:
            return services.assign_to_project(
                admin_principal, project_id=project.pk, user_id=site_manager.pk, kind=kind
            )

        first = assign(ProjectAssignment.Kind.MANAGER)
        with pytest.raises(ValidationError):
            assign(ProjectAssignment.Kind.MANAGER)
        assign(ProjectAssignment.Kind.PARTNER)
        services.revoke_project_assignment(admin_principal, first.pk)
        assign(ProjectAssignment.Kind.MANAGER)

    def test_revoke(
        self,
        admin_principal: SessionPrincipal,
        group_admin: User,
        project: Project,
        site_manager: User,
    ) -> None:
        assignment = services.assign_to_project(
            admin_principal,
            project_id=project.pk,
            user_id=site_manager.pk,
            kind=ProjectAssignment.Kind.PARTNER,
        )
        before = authz_generation(site_manager)
        revoked = services.revoke_project_assignment(admin_principal, assignment.pk)
        assert (revoked.revoked_by, authz_generation(site_manager)) == (group_admin, before + 1)
        with pytest.raises(ValidationError):
            services.revoke_project_assignment(admin_principal, assignment.pk)

    def test_change_validity(
        self, admin_principal: SessionPrincipal, project: Project, site_manager: User
    ) -> None:
        assignment = services.assign_to_project(
            admin_principal,
            project_id=project.pk,
            user_id=site_manager.pk,
            kind=ProjectAssignment.Kind.MANAGER,
        )
        start = assignment.valid_from
        end = start + timedelta(days=7)
        before = authz_generation(site_manager)
        services.change_assignment_validity(
            admin_principal, assignment.pk, valid_from=start, valid_until=end
        )
        assert authz_generation(site_manager) == before + 1
        services.change_assignment_validity(
            admin_principal, assignment.pk, valid_from=start, valid_until=end
        )
        assert AuditEvent.objects.filter(action="project_assignment.validity_changed").count() == 1
        with pytest.raises(ValidationError):
            services.change_assignment_validity(
                admin_principal, assignment.pk, valid_from=end, valid_until=start
            )

    def test_staff_accounts_need_a_superuser(
        self, admin_principal: SessionPrincipal, project: Project, make_user: Callable[..., User]
    ) -> None:
        staff = make_user("ops@example.org", is_superuser=True)
        with pytest.raises(AccessDenied):
            services.assign_to_project(
                admin_principal,
                project_id=project.pk,
                user_id=staff.pk,
                kind=ProjectAssignment.Kind.MANAGER,
            )

    def test_needs_project_manage(
        self, make_user: Callable[..., User], project: Project, site_manager: User
    ) -> None:
        stranger = principal_for(make_user("stranger@example.org"))
        with pytest.raises(AccessDenied):
            services.assign_to_project(
                stranger,
                project_id=project.pk,
                user_id=site_manager.pk,
                kind=ProjectAssignment.Kind.MANAGER,
                valid_from=timezone.now(),
            )
        assert not ProjectAssignment.objects.exists()
