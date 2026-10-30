"""Scope rules S1-S10 of plan §3.5, one class per rule (the org of ``conftest.py``)."""

from collections.abc import Callable
from datetime import timedelta
from uuid import UUID

import pytest
from django.db import transaction
from django.utils import timezone

from apps.access.authorization import authorize_in_txn
from apps.access.context import AccessContext
from apps.access.exceptions import AccessDenied, MFARequired
from apps.access.roles import Capability, Role
from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.audit.recorder import AuditIdentity, record
from apps.core.db import db_now
from apps.core.locking import LockOrder, LockOutsideTransaction
from apps.locations.models import Location
from apps.organizations.models import Branch, Company
from apps.projects.models import Project, ProjectAssignment
from tests.support import principal_for
from tests.unit.access.conftest import HOUR, Org, also, assign

pytestmark = pytest.mark.django_db

VIEW = Capability.PROJECT_VIEW
MANAGER, PARTNER = ProjectAssignment.Kind.MANAGER, ProjectAssignment.Kind.PARTNER
Member = Callable[..., User]


def context(user: User) -> AccessContext:
    return AccessContext.for_user(User.objects.get(pk=user.pk))


def branches(user: User, capability: str = VIEW) -> set[str]:
    return set(Branch.objects.visible_to(context(user), capability).values_list("name", flat=True))


def projects(user: User, capability: str = VIEW) -> set[str]:
    return set(Project.objects.visible_to(context(user), capability).values_list("code", flat=True))


def locations(user: User, capability: str = VIEW) -> set[str]:
    rows = Location.objects.visible_to(context(user), capability)
    return set(rows.values_list("name", flat=True))


def deactivate(obj: Branch | Company) -> None:
    type(obj).objects.filter(pk=obj.pk).update(is_active=False)


class TestS1BranchScope:
    """A branch and the branches it manages, minus inactive ones."""

    @pytest.mark.parametrize("role", [Role.MATERIAL_MANAGER, Role.WAREHOUSE_OPERATOR])
    def test_branch_plus_managed_branches(self, org: Org, member: Member, role: Role) -> None:
        user = member(role, branch=org.a_mad)
        assert branches(user) == {"A Madrid", "B Madrid"}
        assert locations(user) >= {"wh A Madrid", "wh B Madrid"}
        assert "wh A Sevilla" not in locations(user)

    def test_a_managed_branch_reaches_only_itself(self, org: Org, member: Member) -> None:
        user = member(Role.MATERIAL_MANAGER, branch=org.b_mad)
        assert branches(user) == {"B Madrid"}

    def test_inactive_managed_branches_drop_out(self, org: Org, member: Member) -> None:
        user = member(Role.MATERIAL_MANAGER, branch=org.a_mad)
        deactivate(org.b_mad)
        assert branches(user) == {"A Madrid"}

    def test_a_managed_branch_of_an_inactive_company_drops_out(
        self, org: Org, member: Member
    ) -> None:
        user = member(Role.MATERIAL_MANAGER, branch=org.a_mad)
        deactivate(org.b)
        assert branches(user) == {"A Madrid"}
        assert "wh B Madrid" not in locations(user)


class TestS2GroupAdmin:
    def test_everything_including_inactive_history(self, org: Org, member: Member) -> None:
        admin = member(Role.GROUP_ADMIN)
        deactivate(org.b)
        deactivate(org.a_sev)
        assert branches(admin) == set(Branch.objects.values_list("name", flat=True))
        assert projects(admin) == set(Project.objects.values_list("code", flat=True))
        assert locations(admin) == set(Location.objects.values_list("name", flat=True))
        scope = context(admin).scope_for(Capability.ORG_MANAGE)
        assert scope.everything and scope.installation


class TestS3ProjectsOfABranchScope:
    def test_by_branch_or_procurement_branch(self, org: Org, member: Member) -> None:
        mad = member(Role.MATERIAL_MANAGER, branch=org.a_mad)
        sev = member(Role.MATERIAL_MANAGER, branch=org.a_sev)
        assert projects(mad) == {"HOSP-X", "SEV-2", "MAD-1"}
        assert projects(sev) == {"SEV-1", "SEV-2"}

    def test_worksites_follow_their_project(self, org: Org, member: Member) -> None:
        mad = member(Role.MATERIAL_MANAGER, branch=org.a_mad)
        # SEV-2 is executed by A-SEV but bought by A-MAD: its worksite is in A-MAD's scope.
        assert {"site HOSP-X", "site SEV-2", "site MAD-1"} <= locations(mad)
        assert "site SEV-1" not in locations(mad)

    def test_technicians_see_only_active_projects(self, org: Org, member: Member) -> None:
        """Q6: any active project of their branch scope."""
        technician = member(Role.TECHNICIAN, branch=org.a_mad)
        assert projects(technician) == {"HOSP-X", "SEV-2"}
        assert "site MAD-1" not in locations(technician)
        assert projects(technician, Capability.REQUEST_CREATE) == {"HOSP-X", "SEV-2"}

    def test_a_project_bought_by_a_branch_in_scope_stays_visible(
        self, org: Org, member: Member
    ) -> None:
        """S9 removes B-MAD; S3 keeps Hospital X through its procurement branch."""
        mad = member(Role.MATERIAL_MANAGER, branch=org.a_mad)
        deactivate(org.b_mad)
        assert "HOSP-X" in projects(mad)


class TestS4AssignedProjects:
    def test_site_managers_see_their_assigned_projects(self, org: Org, member: Member) -> None:
        site = member(Role.SITE_MANAGER, branch=org.a_mad)
        assign(site, org.hospital, MANAGER)
        assign(site, org.mad_hold, MANAGER)
        assert projects(site) == {"HOSP-X", "MAD-1"}  # any status
        assert locations(site) == {"site HOSP-X", "site MAD-1"}
        assert branches(site) == set()  # their home branch grants no branch scope

    def test_partners_see_only_active_assigned_projects(self, org: Org, member: Member) -> None:
        partner = member(Role.PARTNER_REQUESTER, company=org.c)
        assign(partner, org.hospital, PARTNER)
        assign(partner, org.mad_hold, PARTNER)
        assert projects(partner) == {"HOSP-X"}
        assert locations(partner) == {"site HOSP-X"}

    def test_only_valid_assignments_of_the_roles_kind(self, org: Org, member: Member) -> None:
        site = member(Role.SITE_MANAGER, branch=org.a_mad)
        now = timezone.now()
        assign(site, org.hospital, PARTNER)  # another role's kind
        assign(site, org.sev_works, MANAGER, revoked_at=now - HOUR)
        assign(site, org.sev_for_mad, MANAGER, valid_from=now - 2 * HOUR, valid_until=now - HOUR)
        assign(site, org.mad_hold, MANAGER, valid_from=now + HOUR)
        assert projects(site) == set()

    def test_an_assignment_without_the_role_grants_nothing(self, org: Org, member: Member) -> None:
        technician = member(Role.TECHNICIAN, branch=org.a_sev)
        assign(technician, org.hospital, MANAGER)
        assert projects(technician) == {"SEV-1", "SEV-2"}

    def test_projects_of_an_inactive_branch_drop_out(self, org: Org, member: Member) -> None:
        site = member(Role.SITE_MANAGER, branch=org.a_mad)
        assign(site, org.hospital, MANAGER)
        assign(site, org.sev_works, MANAGER)
        deactivate(org.b)
        assert projects(site) == {"SEV-1"}

    def test_no_assignment_no_capability(self, org: Org, member: Member) -> None:
        site = member(Role.SITE_MANAGER, branch=org.a_mad)
        assert not context(site).has(Capability.REQUEST_REVIEW)
        assert context(site).has(Capability.CATALOG_VIEW)  # installation-wide


class TestS5Documents:
    """Documents by branch or project; technicians and partners only their own.

    Audit events stand in for documents until M4: ``branch_id`` (the document's branch),
    ``object_id`` (its project) and ``actor`` (its creator).
    """

    @pytest.fixture
    def documents(self, org: Org, make_user: Callable[..., User]) -> dict[str, UUID]:
        author, other = make_user("author@example.org"), make_user("other@example.org")
        rows = {
            "own-mad": (author, org.a_mad.pk, org.mad_hold),
            "other-mad": (other, org.a_mad.pk, org.mad_hold),
            "own-sev": (author, org.a_sev.pk, org.sev_works),
            "own-hospital": (author, org.b_mad.pk, org.hospital),
            "other-hospital": (other, org.b_mad.pk, org.hospital),
        }
        ids = {}
        with transaction.atomic():
            for name, (user, branch_id, project) in rows.items():
                event = record(
                    AuditIdentity(label=user.email, user_id=user.pk),
                    "document.created",
                    obj=project,
                    branch_id=branch_id,
                    data={"name": name},
                )
                ids[name] = event.pk
        return ids

    def visible(self, user: User, capability: Capability) -> set[str]:
        scope = context(user).scope_for(capability)
        rows = AuditEvent.objects.filter(
            scope.document_q(branch="branch_id", project="object_id", owner="actor")
        )
        return {row.data["name"] for row in rows}

    def test_by_branch_for_branch_roles(
        self, org: Org, member: Member, documents: dict[str, UUID]
    ) -> None:
        manager = member(Role.MATERIAL_MANAGER, branch=org.a_mad)
        assert self.visible(manager, Capability.REQUEST_VIEW) == {
            "own-mad",
            "other-mad",
            "own-hospital",
            "other-hospital",
        }

    def test_technicians_see_only_their_own(
        self, org: Org, member: Member, documents: dict[str, UUID]
    ) -> None:
        author = User.objects.get(email="author@example.org")
        also(author, Role.TECHNICIAN, branch=org.a_mad)
        assert self.visible(author, Capability.REQUEST_VIEW) == {"own-mad", "own-hospital"}

    def test_partners_see_only_their_own_on_assigned_projects(
        self, org: Org, documents: dict[str, UUID]
    ) -> None:
        author = User.objects.get(email="author@example.org")
        also(author, Role.PARTNER_REQUESTER, company=org.c)
        assign(author, org.hospital, PARTNER)
        assert self.visible(author, Capability.REQUEST_VIEW) == {"own-hospital"}

    def test_site_managers_see_every_document_of_their_projects(
        self, org: Org, member: Member, documents: dict[str, UUID]
    ) -> None:
        site = member(Role.SITE_MANAGER, branch=org.a_sev)
        assign(site, org.hospital, MANAGER)
        assert self.visible(site, Capability.REQUEST_VIEW) == {"own-hospital", "other-hospital"}

    def test_group_admins_see_all_and_create_only_their_own(
        self, member: Member, documents: dict[str, UUID]
    ) -> None:
        admin = member(Role.GROUP_ADMIN)
        assert len(self.visible(admin, Capability.REQUEST_VIEW)) == len(documents)
        assert self.visible(admin, Capability.REQUEST_CREATE) == set()


class TestS6NoMixingAcrossMemberships:
    """``MATERIAL_MANAGER`` in A-SEV and ``TECHNICIAN`` in A-MAD (plan §3.5, S6)."""

    @pytest.fixture
    def mixed(self, org: Org, member: Member) -> User:
        user = member(Role.MATERIAL_MANAGER, branch=org.a_sev)
        also(user, Role.TECHNICIAN, branch=org.a_mad)
        return user

    def test_each_capability_stays_in_its_memberships_scope(self, org: Org, mixed: User) -> None:
        assert projects(mixed, Capability.REQUEST_REVIEW) == {"SEV-1", "SEV-2"}
        assert projects(mixed, Capability.PROJECT_MANAGE) == {"SEV-1", "SEV-2"}
        # project.view: all of A-SEV (manager) plus A-MAD's active projects (technician).
        assert projects(mixed) == {"SEV-1", "SEV-2", "HOSP-X"}
        grants = context(mixed).scope_for(Capability.REQUEST_CREATE).grants
        assert {grant.role for grant in grants} == {Role.MATERIAL_MANAGER, Role.TECHNICIAN}

    def test_cannot_approve_in_the_technicians_branch(self, org: Org, mixed: User) -> None:
        principal = principal_for(mixed)
        with transaction.atomic():
            authorize_in_txn(principal, Capability.REQUEST_REVIEW, org.sev_works, locks=LockOrder())
        with transaction.atomic(), pytest.raises(AccessDenied):
            authorize_in_txn(principal, Capability.REQUEST_REVIEW, org.hospital, locks=LockOrder())

    def test_mfa_only_where_a_privileged_grant_is_used(self, org: Org, mixed: User) -> None:
        """The technician's grant needs no MFA; the manager's does (plan §3.6, step 5)."""
        principal = principal_for(mixed, mfa_at=None)
        with transaction.atomic():
            authorize_in_txn(principal, Capability.REQUEST_CREATE, org.hospital, locks=LockOrder())
        with transaction.atomic(), pytest.raises(MFARequired):
            authorize_in_txn(principal, Capability.REQUEST_CREATE, org.sev_works, locks=LockOrder())


class TestS8Validity:
    @pytest.mark.parametrize(
        "window",
        [
            pytest.param({"revoked_at": timezone.now() - HOUR}, id="revoked"),
            pytest.param(
                {"valid_from": timezone.now() - 2 * HOUR, "valid_until": timezone.now() - HOUR},
                id="expired",
            ),
            pytest.param({"valid_from": timezone.now() + HOUR}, id="not-yet-valid"),
        ],
    )
    def test_invalid_memberships_grant_nothing(
        self, org: Org, member: Member, window: dict[str, object]
    ) -> None:
        user = member(Role.MATERIAL_MANAGER, branch=org.a_mad, **window)
        assert not context(user).has(VIEW)
        assert branches(user) == set()

    def test_validity_is_half_open_at_the_database_clock(self, org: Org, member: Member) -> None:
        with transaction.atomic():
            now = db_now()
            ended = member(Role.MATERIAL_MANAGER, branch=org.a_mad, valid_from=now - HOUR)
            ended.memberships.update(valid_until=now)
            started = member(Role.MATERIAL_MANAGER, branch=org.a_sev, valid_from=now)
            assert branches(ended) == set()
            assert branches(started) == {"A Sevilla"}

    @pytest.mark.parametrize(
        "change", [{"is_active": False}, {"offboarded_at": timezone.now()}], ids=str
    )
    def test_inactive_or_offboarded_users_reach_nothing(
        self, org: Org, member: Member, change: dict[str, object]
    ) -> None:
        user = member(Role.GROUP_ADMIN)
        User.objects.filter(pk=user.pk).update(**change)
        assert branches(user) == set()
        assert not context(user).has(Capability.ORG_MANAGE)

    def test_anonymous_reaches_nothing(self, org: Org) -> None:
        anonymous = AccessContext.anonymous()
        assert not anonymous.is_authenticated and anonymous.user_id is None
        assert not Branch.objects.visible_to(anonymous, VIEW).exists()
        assert not anonymous.has(Capability.CATALOG_VIEW)


class TestS9OrganizationState:
    def test_an_inactive_branch_grants_nothing_through_its_memberships(
        self, org: Org, member: Member
    ) -> None:
        user = member(Role.MATERIAL_MANAGER, branch=org.b_mad)
        deactivate(org.b_mad)
        assert not context(user).has(VIEW)
        assert not context(user).has(Capability.CATALOG_VIEW)

    def test_an_inactive_company_makes_its_branches_inactive(
        self, org: Org, member: Member
    ) -> None:
        user = member(Role.MATERIAL_MANAGER, branch=org.a_mad)
        site = member(Role.SITE_MANAGER, branch=org.a_mad)
        assign(site, org.sev_works, MANAGER)
        deactivate(org.a)
        assert not context(user).has(VIEW)
        assert not context(site).has(VIEW)

    def test_a_partner_of_an_inactive_company_alone(self, org: Org, member: Member) -> None:
        """F50: the partner membership and the assignments that depend on it."""
        partner = member(Role.PARTNER_REQUESTER, company=org.c)
        assign(partner, org.hospital, PARTNER)
        assert projects(partner) == {"HOSP-X"}
        deactivate(org.c)
        assert projects(partner) == set()
        for capability in Capability:
            assert not context(partner).has(capability)

    def test_a_partner_of_an_inactive_company_with_a_mixed_membership(
        self, org: Org, member: Member
    ) -> None:
        partner = member(Role.PARTNER_REQUESTER, company=org.c)
        assign(partner, org.sev_works, PARTNER)
        also(partner, Role.TECHNICIAN, branch=org.a_mad)
        deactivate(org.c)
        # The partner grant is gone; the technician's own membership still applies.
        assert projects(partner) == {"HOSP-X", "SEV-2"}
        grants = context(partner).scope_for(Capability.REQUEST_CREATE).grants
        assert {grant.role for grant in grants} == {Role.TECHNICIAN}

    def test_only_group_admins_keep_inactive_history(self, org: Org, member: Member) -> None:
        manager = member(Role.MATERIAL_MANAGER, branch=org.a_mad)
        admin = member(Role.GROUP_ADMIN)
        deactivate(org.b)
        assert "B Madrid" not in branches(manager)
        assert "B Madrid" in branches(admin)


class TestS10WritesAuthorizeInTheirTransaction:
    @pytest.mark.django_db(transaction=True)
    def test_outside_a_transaction_is_refused(self, member: Member) -> None:
        admin = member(Role.GROUP_ADMIN)
        with pytest.raises(LockOutsideTransaction):
            authorize_in_txn(principal_for(admin), Capability.ORG_MANAGE, locks=LockOrder())

    def test_each_call_reloads_the_memberships(self, org: Org, member: Member) -> None:
        """No cached answer: a revocation inside the same request is seen at once."""
        manager = member(Role.MATERIAL_MANAGER, branch=org.a_mad)
        principal = principal_for(manager)
        with transaction.atomic():
            authorize_in_txn(principal, VIEW, org.hospital, locks=LockOrder())
        manager.memberships.update(revoked_at=timezone.now() - timedelta(seconds=1))
        with transaction.atomic(), pytest.raises(AccessDenied):
            authorize_in_txn(principal, VIEW, org.hospital, locks=LockOrder())
