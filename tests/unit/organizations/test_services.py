"""Organization services: companies, joint ventures, branches, memberships (plan §3.3, §3.6)."""

from collections.abc import Callable, Collection, Iterator
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.access.authorization import AccessDenied
from apps.access.generations import org_generation
from apps.access.principals import SessionPrincipal
from apps.access.roles import Role
from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.core import events
from apps.core.idempotency import IdempotencyConflict, issue_key
from apps.locations.models import Location
from apps.organizations import services
from apps.organizations.deactivation import (
    DeactivationRefused,
    OpenDocument,
    open_documents,
    register_open_documents,
)
from apps.organizations.models import Branch, Company, JointVentureMember, Membership
from tests.support import authz_generation, grant, principal_for

pytestmark = pytest.mark.django_db

HOUR = timedelta(hours=1)


@pytest.fixture
def published() -> Iterator[list[events.Event]]:
    seen: list[events.Event] = []
    names = (
        "company.created",
        "company.updated",
        "branch.updated",
        "membership.granted",
        "membership.revoked",
    )
    undo = [events.subscribe(name, seen.append) for name in names]
    yield seen
    for unsubscribe in undo:
        unsubscribe()


@pytest.fixture
def blocking_documents() -> Iterator[list[OpenDocument]]:
    """A document provider (as M4/M5 register) whose open documents are set by the test."""
    documents: list[OpenDocument] = []
    seen_branches: list[Collection[UUID]] = []

    def provider(branch_ids: Collection[UUID]) -> list[OpenDocument]:
        seen_branches.append(branch_ids)
        return documents

    unregister = register_open_documents(provider)
    yield documents
    unregister()


@pytest.fixture
def outsider(make_user: Callable[..., User], branch: Branch) -> SessionPrincipal:
    """A material manager: holds ``project.manage`` in M2.3, never ``org.manage``."""
    user = make_user("mm@example.org")
    grant(user, Role.MATERIAL_MANAGER, branch=branch)
    return principal_for(user)


def actions() -> list[str]:
    return list(AuditEvent.objects.order_by("occurred_at", "id").values_list("action", flat=True))


class TestCompanies:
    def test_create_adds_the_head_office_and_audits(
        self, admin_principal: SessionPrincipal, published: list[events.Event]
    ) -> None:
        before = org_generation()
        company = services.create_company(
            admin_principal, code="C", legal_name="Partner C", tax_id="B12345678"
        )
        head_office = Branch.objects.get(company=company)
        assert (head_office.code, head_office.name) == (services.HEAD_OFFICE_CODE, "Partner C")
        assert company.kind == Company.Kind.STANDARD
        assert actions()[-2:] == ["company.created", "branch.created"]
        event = AuditEvent.objects.get(action="company.created")
        assert event.actor_id == admin_principal.user_id
        assert event.data["code"] == "C"
        assert [e.name for e in published] == ["company.created"]
        assert org_generation() > before

    def test_create_validates(self, admin_principal: SessionPrincipal) -> None:
        with pytest.raises(ValidationError) as excinfo:
            services.create_company(admin_principal, code="bad code", legal_name="X")
        assert "code" in excinfo.value.message_dict
        assert not Company.objects.exists()

    def test_create_with_a_key_runs_once(self, admin_principal: SessionPrincipal) -> None:
        key = issue_key(admin_principal.user_id, "company.create")
        first = services.create_company(
            admin_principal, code="C", legal_name="C", idempotency_key=key
        )
        again = services.create_company(
            admin_principal, code="C", legal_name="C", idempotency_key=key
        )
        assert again == first
        assert Company.objects.count() == 1
        with pytest.raises(IdempotencyConflict):
            services.create_company(admin_principal, code="D", legal_name="D", idempotency_key=key)

    def test_update_changes_data_and_audits_the_diff(
        self, admin_principal: SessionPrincipal, company: Company
    ) -> None:
        before = org_generation()
        updated = services.update_company(admin_principal, company.pk, legal_name="A S.A.")
        assert updated.legal_name == "A S.A."
        event = AuditEvent.objects.filter(action="company.updated").get()
        assert event.data == {"legal_name": {"from": "Company A", "to": "A S.A."}}
        assert org_generation() == before  # a name is not scope-defining

    def test_update_without_change_writes_nothing(
        self, admin_principal: SessionPrincipal, company: Company
    ) -> None:
        services.update_company(admin_principal, company.pk, legal_name="Company A")
        assert not AuditEvent.objects.filter(action="company.updated").exists()

    def test_code_is_fixed(self, admin_principal: SessionPrincipal, company: Company) -> None:
        with pytest.raises(ValueError, match="code"):
            services.update_company(admin_principal, company.pk, code="Z")

    def test_update_validates(self, admin_principal: SessionPrincipal, company: Company) -> None:
        with pytest.raises(ValidationError):
            services.update_company(admin_principal, company.pk, kind="OTHER")

    def test_deactivation_bumps_the_generation(
        self, admin_principal: SessionPrincipal, company: Company, published: list[events.Event]
    ) -> None:
        before = org_generation()
        services.update_company(admin_principal, company.pk, is_active=False)
        assert org_generation() == before + 1
        assert published[-1].payload["fields"] == ["is_active"]
        services.update_company(admin_principal, company.pk, is_active=True)
        assert org_generation() == before + 2

    def test_deactivation_is_refused_with_open_documents(
        self,
        admin_principal: SessionPrincipal,
        company: Company,
        branch: Branch,
        blocking_documents: list[OpenDocument],
    ) -> None:
        order = OpenDocument("purchase_order", "PED-A-2026-00001", UUID(int=7))
        request = OpenDocument("material_request", "SOL-MAD-2026-00003", UUID(int=8))
        blocking_documents.extend([order, request])
        with pytest.raises(DeactivationRefused) as excinfo:
            services.update_company(admin_principal, company.pk, is_active=False)
        assert excinfo.value.documents == [request, order]
        assert "PED-A-2026-00001" in excinfo.value.messages[0]
        assert Company.objects.get(pk=company.pk).is_active is True

    def test_a_venture_with_members_stays_a_venture(
        self, admin_principal: SessionPrincipal, company: Company
    ) -> None:
        venture = services.create_company(
            admin_principal, code="B", legal_name="UTE B", kind=Company.Kind.JOINT_VENTURE
        )
        services.add_joint_venture_member(
            admin_principal, venture_id=venture.pk, member_id=company.pk
        )
        with pytest.raises(ValidationError) as excinfo:
            services.update_company(admin_principal, venture.pk, kind=Company.Kind.STANDARD)
        assert "kind" in excinfo.value.message_dict
        empty = services.create_company(
            admin_principal, code="E", legal_name="E", kind=Company.Kind.JOINT_VENTURE
        )
        assert (
            services.update_company(admin_principal, empty.pk, kind=Company.Kind.STANDARD).kind
            == Company.Kind.STANDARD
        )

    def test_needs_org_manage(self, outsider: SessionPrincipal, company: Company) -> None:
        with pytest.raises(AccessDenied):
            services.create_company(outsider, code="X", legal_name="X")
        with pytest.raises(AccessDenied):
            services.update_company(outsider, company.pk, legal_name="X")
        assert Company.objects.get(pk=company.pk).legal_name == "Company A"

    def test_a_revoked_admin_is_refused(
        self, admin_principal: SessionPrincipal, group_admin: User
    ) -> None:
        Membership.objects.filter(user=group_admin).update(revoked_at=timezone.now() - HOUR)
        with pytest.raises(AccessDenied):
            services.create_company(admin_principal, code="X", legal_name="X")


class TestJointVentures:
    @pytest.fixture
    def venture(self, admin_principal: SessionPrincipal) -> Company:
        return services.create_company(
            admin_principal, code="B", legal_name="UTE B", kind=Company.Kind.JOINT_VENTURE
        )

    def test_add_and_update_members(
        self, admin_principal: SessionPrincipal, venture: Company, company: Company
    ) -> None:
        key = issue_key(admin_principal.user_id, "joint_venture_member.add")
        member = services.add_joint_venture_member(
            admin_principal,
            venture_id=venture.pk,
            member_id=company.pk,
            share_pct=Decimal("60.00"),
            idempotency_key=key,
        )
        assert member.venture_kind == Company.Kind.JOINT_VENTURE
        updated = services.update_joint_venture_member(
            admin_principal, member.pk, share_pct=Decimal("55.50")
        )
        assert updated.share_pct == Decimal("55.50")
        services.update_joint_venture_member(admin_principal, member.pk, share_pct=Decimal("55.50"))
        assert actions().count("joint_venture_member.updated") == 1
        assert str(member)

    def test_only_a_venture_has_members(
        self, admin_principal: SessionPrincipal, company: Company, venture: Company
    ) -> None:
        with pytest.raises(ValidationError) as excinfo:
            services.add_joint_venture_member(
                admin_principal, venture_id=company.pk, member_id=venture.pk
            )
        assert "venture" in excinfo.value.message_dict

    def test_member_rules_are_validated(
        self, admin_principal: SessionPrincipal, venture: Company, company: Company
    ) -> None:
        with pytest.raises(ValidationError):
            services.add_joint_venture_member(
                admin_principal, venture_id=venture.pk, member_id=venture.pk
            )
        with pytest.raises(ValidationError):
            services.add_joint_venture_member(
                admin_principal,
                venture_id=venture.pk,
                member_id=company.pk,
                share_pct=Decimal("120"),
            )

    def test_needs_org_manage(
        self, outsider: SessionPrincipal, venture: Company, company: Company
    ) -> None:
        with pytest.raises(AccessDenied):
            services.add_joint_venture_member(outsider, venture_id=venture.pk, member_id=company.pk)
        assert not JointVentureMember.objects.exists()


class TestBranches:
    def test_create(self, admin_principal: SessionPrincipal, company: Company) -> None:
        before = org_generation()
        key = issue_key(admin_principal.user_id, "branch.create")
        branch = services.create_branch(
            admin_principal,
            company_id=company.pk,
            code="SEV",
            name="Sevilla",
            allow_self_approval=True,
            idempotency_key=key,
        )
        assert branch.allow_self_approval is True
        assert org_generation() > before
        assert AuditEvent.objects.get(action="branch.created", object_id=branch.pk).branch_id == (
            branch.pk
        )
        assert str(branch) == "A-SEV · Sevilla"

    def test_not_in_an_inactive_company(
        self, admin_principal: SessionPrincipal, company: Company
    ) -> None:
        services.update_company(admin_principal, company.pk, is_active=False)
        with pytest.raises(ValidationError) as excinfo:
            services.create_branch(admin_principal, company_id=company.pk, code="X", name="X")
        assert "company" in excinfo.value.message_dict

    def test_code_is_unique_per_company(
        self, admin_principal: SessionPrincipal, company: Company, branch: Branch
    ) -> None:
        with pytest.raises(ValidationError):
            services.create_branch(admin_principal, company_id=company.pk, code="MAD", name="X")
        other = services.create_company(admin_principal, code="B", legal_name="B")
        services.create_branch(admin_principal, company_id=other.pk, code="MAD", name="B Madrid")

    def test_managed_by_bumps_and_stays_one_level(
        self, admin_principal: SessionPrincipal, company: Company, branch: Branch
    ) -> None:
        venture = services.create_company(
            admin_principal, code="B", legal_name="UTE B", kind=Company.Kind.JOINT_VENTURE
        )
        b_mad = services.create_branch(
            admin_principal, company_id=venture.pk, code="MAD", name="B Madrid"
        )
        before = org_generation()
        services.update_branch(admin_principal, b_mad.pk, managed_by_id=branch.pk)
        assert org_generation() == before + 1
        hq = Branch.objects.get(company=company, code=services.HEAD_OFFICE_CODE)
        with pytest.raises(ValidationError) as excinfo:
            services.update_branch(admin_principal, hq.pk, managed_by_id=b_mad.pk)
        assert excinfo.value.error_dict["managed_by"][0].code == "managed_by_two_levels"
        with pytest.raises(ValidationError) as excinfo:
            services.update_branch(admin_principal, branch.pk, managed_by_id=hq.pk)
        assert excinfo.value.error_dict["managed_by"][0].code == "managed_by_two_levels"
        with pytest.raises(ValidationError) as excinfo:
            services.update_branch(admin_principal, hq.pk, managed_by_id=hq.pk)
        assert excinfo.value.error_dict["managed_by"][0].code == "managed_by_self"

    def test_default_warehouse_is_an_active_warehouse_of_the_branch(
        self, admin_principal: SessionPrincipal, company: Company, branch: Branch
    ) -> None:
        own = Location.objects.create(kind=Location.Kind.WAREHOUSE, branch=branch, name="Almacén")
        hq = Branch.objects.get(company=company, code=services.HEAD_OFFICE_CODE)
        foreign = Location.objects.create(kind=Location.Kind.WAREHOUSE, branch=hq, name="HQ")
        closed = Location.objects.create(
            kind=Location.Kind.WAREHOUSE, branch=branch, name="Old", is_active=False
        )
        for location in (foreign, closed):
            with pytest.raises(ValidationError) as excinfo:
                services.update_branch(admin_principal, branch.pk, default_warehouse_id=location.pk)
            assert "default_warehouse" in excinfo.value.message_dict
        before = org_generation()
        updated = services.update_branch(admin_principal, branch.pk, default_warehouse_id=own.pk)
        assert updated.default_warehouse_id == own.pk
        assert org_generation() == before  # not scope-defining
        services.update_branch(admin_principal, branch.pk, default_warehouse_id=None)

    def test_deactivation_bumps_or_is_refused(
        self,
        admin_principal: SessionPrincipal,
        branch: Branch,
        blocking_documents: list[OpenDocument],
    ) -> None:
        blocking_documents.append(OpenDocument("purchase_order", "PED-A-2026-00002", UUID(int=9)))
        with pytest.raises(DeactivationRefused):
            services.update_branch(admin_principal, branch.pk, is_active=False)
        blocking_documents.clear()
        before = org_generation()
        assert (
            services.update_branch(admin_principal, branch.pk, is_active=False).is_active is False
        )
        assert org_generation() == before + 1

    def test_update_without_change_writes_nothing(
        self, admin_principal: SessionPrincipal, branch: Branch
    ) -> None:
        services.update_branch(admin_principal, branch.pk, name="Madrid")
        assert not AuditEvent.objects.filter(action="branch.updated").exists()

    def test_company_and_code_are_fixed(
        self, admin_principal: SessionPrincipal, branch: Branch
    ) -> None:
        with pytest.raises(ValueError, match="company_id"):
            services.update_branch(admin_principal, branch.pk, company_id=branch.pk)

    def test_needs_org_manage(
        self, outsider: SessionPrincipal, company: Company, branch: Branch
    ) -> None:
        with pytest.raises(AccessDenied):
            services.create_branch(outsider, company_id=company.pk, code="X", name="X")
        with pytest.raises(AccessDenied):
            services.update_branch(outsider, branch.pk, is_active=False)


class TestDeactivationRegistry:
    def test_no_branches_no_documents(self, blocking_documents: list[OpenDocument]) -> None:
        blocking_documents.append(OpenDocument("x", "1", UUID(int=1)))
        assert open_documents([]) == []

    def test_registering_twice_has_no_effect(self) -> None:
        def provider(branch_ids: Collection[UUID]) -> list[OpenDocument]:
            return [OpenDocument("x", "1", UUID(int=1))]

        unregister = register_open_documents(provider)
        register_open_documents(provider)
        try:
            assert open_documents([UUID(int=5)]) == [OpenDocument("x", "1", UUID(int=1))]
        finally:
            unregister()
            unregister()
        assert open_documents([UUID(int=5)]) == []


class TestMemberships:
    @pytest.fixture
    def technician(self, make_user: Callable[..., User]) -> User:
        return make_user("tech@example.org")

    def test_grant_bumps_the_users_generation_and_audits(
        self,
        admin_principal: SessionPrincipal,
        technician: User,
        branch: Branch,
        published: list[events.Event],
    ) -> None:
        before = authz_generation(technician)
        key = issue_key(admin_principal.user_id, "membership.grant")
        membership = services.grant_membership(
            admin_principal,
            user_id=technician.pk,
            role=Role.TECHNICIAN,
            branch_id=branch.pk,
            idempotency_key=key,
        )
        assert authz_generation(technician) == before + 1
        assert membership.valid_from is not None and membership.valid_until is None
        event = AuditEvent.objects.get(action="membership.granted")
        assert event.data["role"] == Role.TECHNICIAN
        assert event.branch_id == branch.pk
        assert published[-1].payload["user_id"] == technician.pk
        assert str(membership).endswith("TECHNICIAN")
        technician.refresh_from_db()
        assert technician.privilege_elevated_at is None  # not a privileged role

    @pytest.mark.parametrize(
        ("role", "shape"),
        [
            (Role.GROUP_ADMIN, {"branch": True}),
            (Role.MATERIAL_MANAGER, {}),
            (Role.SITE_MANAGER, {"company": True}),
            (Role.PARTNER_REQUESTER, {"branch": True}),
        ],
    )
    def test_shape_per_role(
        self,
        admin_principal: SessionPrincipal,
        technician: User,
        company: Company,
        branch: Branch,
        role: Role,
        shape: dict[str, bool],
    ) -> None:
        with pytest.raises(ValidationError):
            services.grant_membership(
                admin_principal,
                user_id=technician.pk,
                role=role,
                branch_id=branch.pk if shape.get("branch") else None,
                company_id=company.pk if shape.get("company") else None,
            )

    def test_validity_must_be_a_window(
        self, admin_principal: SessionPrincipal, technician: User, branch: Branch
    ) -> None:
        now = timezone.now()
        with pytest.raises(ValidationError):
            services.grant_membership(
                admin_principal,
                user_id=technician.pk,
                role=Role.TECHNICIAN,
                branch_id=branch.pk,
                valid_from=now,
                valid_until=now,
            )

    def test_privileged_roles_elevate(
        self, admin_principal: SessionPrincipal, technician: User, branch: Branch
    ) -> None:
        later = timezone.now() + timedelta(days=2)
        services.grant_membership(
            admin_principal,
            user_id=technician.pk,
            role=Role.MATERIAL_MANAGER,
            branch_id=branch.pk,
            valid_from=later,
        )
        technician.refresh_from_db()
        assert technician.privilege_elevated_at == later
        services.grant_membership(admin_principal, user_id=technician.pk, role=Role.GROUP_ADMIN)
        technician.refresh_from_db()
        assert technician.privilege_elevated_at == later  # never moves back

    def test_inactive_branch_or_company_is_refused(
        self,
        admin_principal: SessionPrincipal,
        technician: User,
        company: Company,
        branch: Branch,
    ) -> None:
        services.update_branch(admin_principal, branch.pk, is_active=False)
        with pytest.raises(ValidationError) as excinfo:
            services.grant_membership(
                admin_principal, user_id=technician.pk, role=Role.TECHNICIAN, branch_id=branch.pk
            )
        assert "branch" in excinfo.value.message_dict
        hq = Branch.objects.get(company=company, code=services.HEAD_OFFICE_CODE)
        services.update_company(admin_principal, company.pk, is_active=False)
        with pytest.raises(ValidationError):
            services.grant_membership(
                admin_principal, user_id=technician.pk, role=Role.TECHNICIAN, branch_id=hq.pk
            )

    def test_partner_membership_is_on_the_employer(
        self, admin_principal: SessionPrincipal, make_user: Callable[..., User], company: Company
    ) -> None:
        partner_co = services.create_company(admin_principal, code="C", legal_name="C")
        partner = make_user("partner@example.org")
        grant_partner = lambda: services.grant_membership(  # noqa: E731
            admin_principal,
            user_id=partner.pk,
            role=Role.PARTNER_REQUESTER,
            company_id=partner_co.pk,
        )
        with pytest.raises(ValidationError) as excinfo:
            grant_partner()
        assert excinfo.value.error_dict["company"][0].code == "not_employer"
        services.set_employer_company(admin_principal, partner.pk, partner_co.pk)
        assert grant_partner().company_id == partner_co.pk
        services.update_company(admin_principal, partner_co.pk, is_active=False)
        with pytest.raises(ValidationError) as excinfo:
            grant_partner()
        assert excinfo.value.error_dict["company"][0].code == "inactive"

    def test_revoke(
        self,
        admin_principal: SessionPrincipal,
        group_admin: User,
        technician: User,
        branch: Branch,
    ) -> None:
        membership = grant(technician, Role.TECHNICIAN, branch=branch)
        before = authz_generation(technician)
        revoked = services.revoke_membership(admin_principal, membership.pk)
        assert revoked.revoked_by == group_admin
        assert revoked.revoked_at is not None
        assert authz_generation(technician) == before + 1
        assert Membership.objects.filter(pk=membership.pk).exists()  # never deleted
        with pytest.raises(ValidationError):
            services.revoke_membership(admin_principal, membership.pk)

    def test_change_validity(
        self, admin_principal: SessionPrincipal, technician: User, branch: Branch
    ) -> None:
        membership = grant(technician, Role.MATERIAL_MANAGER, branch=branch)
        start = membership.valid_from
        end = start + timedelta(days=30)
        before = authz_generation(technician)
        changed = services.change_membership_validity(
            admin_principal, membership.pk, valid_from=start, valid_until=end
        )
        assert changed.valid_until == end
        assert authz_generation(technician) == before + 1
        technician.refresh_from_db()
        assert technician.privilege_elevated_at is not None
        services.change_membership_validity(
            admin_principal, membership.pk, valid_from=start, valid_until=end
        )
        assert actions().count("membership.validity_changed") == 1
        with pytest.raises(ValidationError):
            services.change_membership_validity(
                admin_principal, membership.pk, valid_from=end, valid_until=start
            )
        services.revoke_membership(admin_principal, membership.pk)
        with pytest.raises(ValidationError):
            services.change_membership_validity(
                admin_principal, membership.pk, valid_from=start, valid_until=None
            )

    def test_validity_of_an_ordinary_role_does_not_elevate(
        self, admin_principal: SessionPrincipal, technician: User, branch: Branch
    ) -> None:
        membership = grant(technician, Role.TECHNICIAN, branch=branch)
        services.change_membership_validity(
            admin_principal,
            membership.pk,
            valid_from=membership.valid_from,
            valid_until=membership.valid_from + timedelta(days=1),
        )
        technician.refresh_from_db()
        assert technician.privilege_elevated_at is None

    def test_staff_accounts_need_a_superuser(
        self,
        admin_principal: SessionPrincipal,
        group_admin: User,
        make_user: Callable[..., User],
        branch: Branch,
    ) -> None:
        staff = make_user("ops@example.org", is_staff=True)
        with pytest.raises(AccessDenied):
            services.grant_membership(
                admin_principal, user_id=staff.pk, role=Role.TECHNICIAN, branch_id=branch.pk
            )
        membership = grant(staff, Role.TECHNICIAN, branch=branch)
        with pytest.raises(AccessDenied):
            services.revoke_membership(admin_principal, membership.pk)
        with pytest.raises(AccessDenied):
            services.set_employer_company(admin_principal, staff.pk, None)
        User.objects.filter(pk=group_admin.pk).update(is_superuser=True)
        services.revoke_membership(admin_principal, membership.pk)

    def test_needs_org_manage(
        self, outsider: SessionPrincipal, technician: User, branch: Branch
    ) -> None:
        with pytest.raises(AccessDenied):
            services.grant_membership(
                outsider, user_id=technician.pk, role=Role.TECHNICIAN, branch_id=branch.pk
            )
        assert not Membership.objects.filter(user=technician).exists()

    def test_a_grant_rolls_back_entirely(
        self, admin_principal: SessionPrincipal, technician: User, branch: Branch
    ) -> None:
        before = authz_generation(technician)

        def fail(event: events.Event) -> None:
            raise RuntimeError("subscriber failed")

        undo = events.subscribe("membership.granted", fail)
        try:
            with pytest.raises(RuntimeError), transaction.atomic():
                services.grant_membership(
                    admin_principal,
                    user_id=technician.pk,
                    role=Role.TECHNICIAN,
                    branch_id=branch.pk,
                )
        finally:
            undo()
        assert authz_generation(technician) == before
        assert not AuditEvent.objects.filter(action="membership.granted").exists()


class TestEmployer:
    def test_set_and_clear(
        self, admin_principal: SessionPrincipal, make_user: Callable[..., User], company: Company
    ) -> None:
        user = make_user()
        before = authz_generation(user)
        assert services.set_employer_company(
            admin_principal, user.pk, company.pk
        ).employer_company == (company)
        assert authz_generation(user) == before + 1
        services.set_employer_company(admin_principal, user.pk, company.pk)
        assert actions().count("user.employer_changed") == 1
        services.set_employer_company(admin_principal, user.pk, None)
        assert User.objects.get(pk=user.pk).employer_company_id is None

    def test_inactive_company_is_refused(
        self, admin_principal: SessionPrincipal, make_user: Callable[..., User], company: Company
    ) -> None:
        services.update_company(admin_principal, company.pk, is_active=False)
        with pytest.raises(ValidationError):
            services.set_employer_company(admin_principal, make_user().pk, company.pk)

    def test_open_partner_memberships_pin_the_employer(
        self, admin_principal: SessionPrincipal, make_user: Callable[..., User], company: Company
    ) -> None:
        partner = make_user("partner@example.org")
        services.set_employer_company(admin_principal, partner.pk, company.pk)
        membership = grant(partner, Role.PARTNER_REQUESTER, company=company)
        other = services.create_company(admin_principal, code="C", legal_name="C")
        for target in (other.pk, None):
            with pytest.raises(ValidationError) as excinfo:
                services.set_employer_company(admin_principal, partner.pk, target)
            assert excinfo.value.error_dict["employer_company"][0].code == "partner_memberships"
        services.revoke_membership(admin_principal, membership.pk)
        services.set_employer_company(admin_principal, partner.pk, other.pk)
