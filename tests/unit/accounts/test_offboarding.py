"""Offboarding and the loss of ``org.manage`` (plan §5, "Offboarding", F41, F71)."""

from collections.abc import Callable
from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.access.exceptions import AccessDenied
from apps.access.principals import SessionPrincipal
from apps.access.roles import Role
from apps.accounts import services
from apps.accounts.models import EmailChangeRequest, Invitation, User
from apps.audit.models import AuditEvent
from apps.notifications.models import EmailOutbox, Kind, Status
from apps.notifications.outbox import queue_security_email
from apps.organizations import services as org_services
from apps.organizations.models import Branch, Membership
from apps.projects.models import ProjectAssignment
from apps.projects.services import assign_to_project, create_project
from tests.support import authz_generation, error_codes, grant, principal_for

pytestmark = pytest.mark.django_db


@pytest.fixture
def second_admin(make_user: Callable[..., User]) -> User:
    admin = make_user("second-admin@example.org")
    grant(admin, Role.GROUP_ADMIN)
    return admin


class TestOffboard:
    def test_one_transaction_does_everything(
        self,
        admin_principal: SessionPrincipal,
        second_admin: User,
        member: Callable[..., User],
        branch: Branch,
        invite: Callable[..., Invitation],
    ) -> None:
        site = member(Role.SITE_MANAGER, email="site@example.org", branch=branch)
        project = create_project(admin_principal, code="P-1", name="P", branch_id=branch.pk)
        assign_to_project(admin_principal, project_id=project.pk, user_id=site.pk, kind="MANAGER")
        theirs = invite("friend@example.org", principal=principal_for(second_admin))
        mine = invite("site@example.org", role=Role.TECHNICIAN)
        change = services.request_email_change(
            principal_for(second_admin), site.pk, new_email="site-new@example.org"
        )
        initiated = services.request_email_change(
            principal_for(second_admin),
            member(Role.TECHNICIAN, email="t@example.org", branch=branch).pk,
            new_email="t-new@example.org",
        )
        with transaction.atomic():
            reset = queue_security_email(
                address=site.email, target=site, rule="account.active", subject="s", body="b"
            )
        epoch, generation = site.session_epoch, authz_generation(site)

        services.offboard_user(admin_principal, site.pk)
        site.refresh_from_db()
        assert not site.is_active and site.offboarded_at is not None
        assert site.session_epoch > epoch and authz_generation(site) > generation
        assert not Membership.objects.filter(user=site, revoked_at__isnull=True).exists()
        assert not ProjectAssignment.objects.filter(user=site, revoked_at__isnull=True).exists()
        assert Invitation.objects.get(pk=mine.pk).revoked_at is not None
        assert Invitation.objects.get(pk=theirs.pk).revoked_at is None  # not theirs to lose
        assert EmailChangeRequest.objects.get(pk=change.pk).revoked_at is not None
        assert EmailChangeRequest.objects.get(pk=initiated.pk).revoked_at is None
        assert EmailOutbox.objects.get(pk=reset.pk).status == Status.EXPIRED
        assert EmailOutbox.objects.get(pk=reset.pk).body == ""
        # Mail about the revoked invitation and email change expired too.
        for target in (mine.pk, change.pk):
            assert set(
                EmailOutbox.objects.filter(eligibility_object_id=target).values_list(
                    "status", flat=True
                )
            ) == {Status.EXPIRED}
        event = AuditEvent.objects.get(action="user.offboarded")
        assert event.data["invitations_revoked"] == [str(mine.pk)]

    def test_revokes_what_they_initiated(
        self,
        admin_principal: SessionPrincipal,
        second_admin: User,
        invite: Callable[..., Invitation],
        member: Callable[..., User],
        branch: Branch,
    ) -> None:
        theirs = invite("friend@example.org", principal=principal_for(second_admin))
        target = member(Role.TECHNICIAN, email="t@example.org", branch=branch)
        change = services.request_email_change(
            principal_for(second_admin), target.pk, new_email="t-new@example.org"
        )
        services.offboard_user(admin_principal, second_admin.pk)
        assert Invitation.objects.get(pk=theirs.pk).revoke_reason == services.REASON_OFFBOARDED
        assert EmailChangeRequest.objects.get(pk=change.pk).revoked_at is not None

    def test_refusals(
        self, admin_principal: SessionPrincipal, group_admin: User, make_user: Callable[..., User]
    ) -> None:
        with pytest.raises(ValidationError) as exc:
            services.offboard_user(admin_principal, group_admin.pk)
        assert error_codes(exc) == {"self"}
        user = make_user("x@example.org")
        services.offboard_user(admin_principal, user.pk)
        with pytest.raises(ValidationError) as exc:
            services.offboard_user(admin_principal, user.pk)
        assert error_codes(exc) == {"offboarded"}
        staff = make_user("op@example.org", is_staff=True)
        with pytest.raises(AccessDenied):
            services.offboard_user(admin_principal, staff.pk)
        assert User.objects.get(pk=staff.pk).is_active

    def test_only_administrators(
        self, member: Callable[..., User], branch: Branch, make_user: Callable[..., User]
    ) -> None:
        manager = member(Role.MATERIAL_MANAGER, branch=branch)
        with pytest.raises(AccessDenied):
            services.offboard_user(principal_for(manager), make_user("x@example.org").pk)


class TestLosingOrgManage:
    def test_revoking_the_admin_membership_revokes_their_tokens(
        self,
        admin_principal: SessionPrincipal,
        second_admin: User,
        invite: Callable[..., Invitation],
        member: Callable[..., User],
        branch: Branch,
    ) -> None:
        theirs = invite("friend@example.org", principal=principal_for(second_admin))
        target = member(Role.TECHNICIAN, email="t@example.org", branch=branch)
        change = services.request_email_change(
            principal_for(second_admin), target.pk, new_email="t-new@example.org"
        )
        membership = Membership.objects.get(user=second_admin)
        org_services.revoke_membership(admin_principal, membership.pk)
        assert Invitation.objects.get(pk=theirs.pk).revoked_at is not None
        assert EmailChangeRequest.objects.get(pk=change.pk).revoked_at is not None
        assert EmailOutbox.objects.filter(
            eligibility_object_id=theirs.pk, status=Status.EXPIRED, kind=Kind.SECURITY
        ).exists()

    def test_moving_the_validity_away_from_now_does_too(
        self,
        admin_principal: SessionPrincipal,
        second_admin: User,
        invite: Callable[..., Invitation],
    ) -> None:
        theirs = invite("friend@example.org", principal=principal_for(second_admin))
        membership = Membership.objects.get(user=second_admin)
        start = timezone.now() + timedelta(days=1)
        org_services.change_membership_validity(
            admin_principal, membership.pk, valid_from=start, valid_until=None
        )
        assert Invitation.objects.get(pk=theirs.pk).revoked_at is not None

    def test_another_valid_admin_membership_keeps_them(
        self,
        admin_principal: SessionPrincipal,
        second_admin: User,
        invite: Callable[..., Invitation],
    ) -> None:
        theirs = invite("friend@example.org", principal=principal_for(second_admin))
        extra = grant(second_admin, Role.GROUP_ADMIN)
        org_services.revoke_membership(admin_principal, extra.pk)
        assert Invitation.objects.get(pk=theirs.pk).revoked_at is None

    def test_other_roles_are_not_checked(
        self, admin_principal: SessionPrincipal, member: Callable[..., User], branch: Branch
    ) -> None:
        tech = member(Role.TECHNICIAN, branch=branch)
        org_services.revoke_membership(admin_principal, Membership.objects.get(user=tech).pk)
        assert not Invitation.objects.exists()
