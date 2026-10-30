"""``authorize_in_txn`` for every principal type (plan §3.6), and admin admission."""

import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Model
from django.http import Http404
from django.utils import timezone

from apps.access.authorization import authorize_in_txn, may_use_admin
from apps.access.context import AccessContext
from apps.access.exceptions import (
    AccessDenied,
    MFARequired,
    NotInScope,
    StaleSession,
    TokenRejected,
)
from apps.access.principals import (
    AnyPrincipal,
    OperatorPrincipal,
    SessionPrincipal,
    SystemPrincipal,
    TokenKind,
    TokenPrincipal,
)
from apps.access.roles import Capability, Role
from apps.accounts.models import User
from apps.core.locking import LockLevel, LockOrder
from tests.org import Org
from tests.support import principal_for

pytestmark = pytest.mark.django_db

Member = Callable[..., User]


def authorize(
    principal: AnyPrincipal, capability: str, *objects: Model, read: str | None = None
) -> Any:
    with transaction.atomic():
        return authorize_in_txn(
            principal,  # type: ignore[arg-type]
            capability,
            *objects,
            locks=LockOrder(),
            read=read,
        )


class TestSessionPrincipal:
    def test_returns_the_actor(self, member: Member) -> None:
        admin = member(Role.GROUP_ADMIN)
        assert authorize(principal_for(admin), Capability.ORG_MANAGE) == admin

    def test_an_object_in_scope(self, org: Org, member: Member) -> None:
        manager = member(Role.MATERIAL_MANAGER, branch=org.a_mad)
        principal = principal_for(manager)
        assert authorize(principal, Capability.PROJECT_MANAGE, org.hospital) == manager
        assert authorize(principal, Capability.PROJECT_MANAGE, org.a_mad, org.b_mad) == manager

    def test_every_object_must_be_in_scope(self, org: Org, member: Member) -> None:
        manager = member(Role.MATERIAL_MANAGER, branch=org.a_mad)
        with pytest.raises(AccessDenied):
            authorize(
                principal_for(manager), Capability.PROJECT_MANAGE, org.hospital, org.sev_works
            )

    def test_s7_invisible_is_404_visible_without_capability_is_403(
        self, org: Org, member: Member
    ) -> None:
        operator = member(Role.WAREHOUSE_OPERATOR, branch=org.a_mad)
        principal = principal_for(operator)
        with pytest.raises(NotInScope):
            authorize(
                principal, Capability.PROJECT_MANAGE, org.sev_works, read=Capability.PROJECT_VIEW
            )
        with pytest.raises(AccessDenied):
            authorize(
                principal, Capability.PROJECT_MANAGE, org.hospital, read=Capability.PROJECT_VIEW
            )
        with pytest.raises(AccessDenied):  # without ``read`` an invisible object is a 403
            authorize(principal, Capability.PROJECT_MANAGE, org.sev_works)
        assert issubclass(NotInScope, Http404)
        assert issubclass(AccessDenied, PermissionDenied)

    def test_unknown_capabilities_are_granted_to_nobody(self, member: Member) -> None:
        with pytest.raises(AccessDenied):
            authorize(principal_for(member(Role.GROUP_ADMIN)), "everything.anything")

    def test_a_superuser_alone_has_no_business_capability(self, make_user: Member) -> None:
        operator = make_user("ops@example.org", is_staff=True, is_superuser=True)
        for capability in Capability:
            with pytest.raises(AccessDenied):
                authorize(principal_for(operator), capability)

    @pytest.mark.parametrize(
        "change", [{"is_active": False}, {"offboarded_at": timezone.now()}], ids=str
    )
    def test_inactive_or_offboarded_users_are_refused(
        self, member: Member, change: dict[str, Any]
    ) -> None:
        admin = member(Role.GROUP_ADMIN)
        principal = principal_for(admin)
        User.objects.filter(pk=admin.pk).update(**change)
        with pytest.raises(AccessDenied):
            authorize(principal, Capability.ORG_MANAGE)

    def test_unknown_users_are_refused(self) -> None:
        ghost = SessionPrincipal(user_id=User(email="ghost@example.org").pk, email="ghost")
        with pytest.raises(AccessDenied):
            authorize(ghost, Capability.CATALOG_VIEW)

    def test_a_session_from_before_an_epoch_bump_is_stale(self, member: Member) -> None:
        admin = member(Role.GROUP_ADMIN)
        principal = principal_for(admin)
        User.objects.filter(pk=admin.pk).update(password="!changed")  # the trigger bumps it
        with pytest.raises(StaleSession):
            authorize(principal, Capability.ORG_MANAGE)
        without_epoch = SessionPrincipal(user_id=admin.pk, email=admin.email, mfa_at=time.time())
        with pytest.raises(StaleSession):
            authorize(without_epoch, Capability.ORG_MANAGE)

    def test_privileged_grants_need_session_mfa(self, org: Org, member: Member) -> None:
        manager = member(Role.MATERIAL_MANAGER, branch=org.a_mad)
        with pytest.raises(MFARequired):
            authorize(principal_for(manager, mfa_at=None), Capability.PROJECT_VIEW, org.hospital)

    def test_mfa_must_follow_the_last_elevation(self, org: Org, member: Member) -> None:
        manager = member(Role.MATERIAL_MANAGER, branch=org.a_mad)
        elevated = timezone.now()
        User.objects.filter(pk=manager.pk).update(privilege_elevated_at=elevated)
        before = principal_for(manager, mfa_at=elevated.timestamp() - 60)
        after = principal_for(manager, mfa_at=elevated.timestamp() + 1)
        with pytest.raises(MFARequired):
            authorize(before, Capability.PROJECT_MANAGE, org.hospital)
        assert authorize(after, Capability.PROJECT_MANAGE, org.hospital) == manager

    def test_ordinary_grants_need_no_mfa(self, org: Org, member: Member) -> None:
        technician = member(Role.TECHNICIAN, branch=org.a_mad)
        principal = principal_for(technician, mfa_at=None)
        assert authorize(principal, Capability.REQUEST_CREATE, org.hospital) == technician

    def test_takes_the_l0_locks(self, member: Member) -> None:
        admin = member(Role.GROUP_ADMIN)
        locks = LockOrder()
        with transaction.atomic():
            authorize_in_txn(principal_for(admin), Capability.ORG_MANAGE, locks=locks)
        assert locks.level is LockLevel.ACCESS


class TestOperatorPrincipal:
    def operator(self, user: User, capability: str) -> OperatorPrincipal:
        return OperatorPrincipal(
            user_id=user.pk, email=user.email, command="import_things", capability=capability
        )

    def test_its_capability_within_scope_without_mfa(self, org: Org, member: Member) -> None:
        manager = member(Role.MATERIAL_MANAGER, branch=org.a_mad)
        operator = self.operator(manager, Capability.PROJECT_MANAGE)
        assert authorize(operator, Capability.PROJECT_MANAGE, org.hospital) == manager
        with pytest.raises(AccessDenied):
            authorize(operator, Capability.PROJECT_MANAGE, org.sev_works)

    def test_only_the_commands_capability(self, member: Member) -> None:
        admin = member(Role.GROUP_ADMIN)
        with pytest.raises(AccessDenied, match=r"catalog\.manage only"):
            authorize(self.operator(admin, Capability.CATALOG_MANAGE), Capability.ORG_MANAGE)

    def test_an_operator_lacking_the_capability(self, org: Org, member: Member) -> None:
        technician = member(Role.TECHNICIAN, branch=org.a_mad)
        with pytest.raises(AccessDenied):
            authorize(
                self.operator(technician, Capability.CATALOG_MANAGE), Capability.CATALOG_MANAGE
            )

    def test_an_inactive_operator(self, member: Member) -> None:
        admin = member(Role.GROUP_ADMIN)
        User.objects.filter(pk=admin.pk).update(is_active=False)
        with pytest.raises(AccessDenied):
            authorize(self.operator(admin, Capability.ORG_MANAGE), Capability.ORG_MANAGE)


class TestTokenPrincipal:
    OPERATION = "invitation.accept"

    def token(
        self,
        target: User | None,
        *,
        initiator: User | None = None,
        check: Callable[[User | None, LockOrder], None] | None = None,
        target_id: UUID | None = None,
        initiator_id: UUID | None = None,
    ) -> TokenPrincipal:
        def accept(found: User | None, locks: LockOrder) -> None:
            assert locks.level is LockLevel.ACCESS  # users locked first, then the token
            self.checked = found

        return TokenPrincipal(
            kind=TokenKind.INVITATION,
            operation=self.OPERATION,
            target_user_id=target_id or (target.pk if target is not None else None),
            target_email=target.email if target is not None else "new@example.org",
            check_token=check or accept,
            token_id=UUID(int=7),
            initiator_user_id=initiator_id or (initiator.pk if initiator is not None else None),
        )

    def test_grants_its_one_operation(self, member: Member, make_user: Member) -> None:
        admin, target = member(Role.GROUP_ADMIN), make_user("new@example.org")
        assert authorize(self.token(target, initiator=admin), self.OPERATION) == target
        assert self.checked == target
        with pytest.raises(AccessDenied, match=r"grants invitation\.accept only"):
            authorize(self.token(target, initiator=admin), Capability.ORG_MANAGE)

    def test_a_new_account_has_no_target_yet(self, member: Member) -> None:
        admin = member(Role.GROUP_ADMIN)
        assert authorize(self.token(None, initiator=admin), self.OPERATION) is None
        assert self.checked is None

    def test_a_token_without_an_initiator(self, make_user: Member) -> None:
        """A password reset or the bootstrap link: the token alone carries the authority."""
        target = make_user()
        assert authorize(self.token(target), self.OPERATION) == target

    def test_the_tokens_own_check_can_refuse(self, make_user: Member) -> None:
        def refuse(found: User | None, locks: LockOrder) -> None:
            raise TokenRejected("used")

        with pytest.raises(TokenRejected):
            authorize(self.token(make_user(), check=refuse), self.OPERATION)

    def test_a_vanished_target_is_refused(self) -> None:
        principal = self.token(None, target_id=User(email="gone@example.org").pk)
        with pytest.raises(TokenRejected):
            authorize(principal, self.OPERATION)

    def test_the_initiator_must_still_hold_org_manage(
        self, member: Member, make_user: Member
    ) -> None:
        """F71: an administrator who lost ``org.manage`` no longer backs their tokens."""
        admin, target = member(Role.GROUP_ADMIN), make_user("new@example.org")
        principal = self.token(target, initiator=admin)
        admin.memberships.update(revoked_at=timezone.now() - timedelta(seconds=1))
        with pytest.raises(TokenRejected):
            authorize(principal, self.OPERATION)
        vanished = self.token(target, initiator_id=User(email="x@example.org").pk)
        with pytest.raises(TokenRejected):
            authorize(vanished, self.OPERATION)

    def test_a_staff_target_needs_a_superuser_initiator(
        self, member: Member, make_user: Member
    ) -> None:
        admin = member(Role.GROUP_ADMIN)
        staff = make_user("ops@example.org", is_staff=True)
        with pytest.raises(AccessDenied):
            authorize(self.token(staff, initiator=admin), self.OPERATION)
        User.objects.filter(pk=admin.pk).update(is_superuser=True)
        assert authorize(self.token(staff, initiator=admin), self.OPERATION) == staff

    def test_is_not_authorized_on_objects(self, org: Org, make_user: Member) -> None:
        with pytest.raises(TypeError):
            authorize(self.token(make_user()), self.OPERATION, org.hospital)


class TestSystemPrincipal:
    @pytest.mark.parametrize("capability", ["outbox.send", "housekeeping.purge_idempotency"])
    def test_its_closed_capability_set(self, capability: str) -> None:
        assert authorize(SystemPrincipal(), capability) is None

    @pytest.mark.parametrize(
        "capability",
        [*(capability.value for capability in Capability), "housekeeping.", "outbox.sendall"],
    )
    def test_never_a_business_capability(self, capability: str) -> None:
        with pytest.raises(AccessDenied):
            authorize(SystemPrincipal(), capability)

    def test_is_not_authorized_on_objects(self, org: Org) -> None:
        with pytest.raises(TypeError):
            authorize(SystemPrincipal(), "outbox.send", read=Capability.PO_VIEW)


class TestMayUseAdmin:
    def test_group_admins_and_platform_operators(self, member: Member, make_user: Member) -> None:
        assert may_use_admin(AccessContext.for_user(member(Role.GROUP_ADMIN))) is True
        operator = make_user("ops@example.org", is_superuser=True)
        assert may_use_admin(AccessContext.for_user(operator)) is True

    def test_nobody_else(self, org: Org, member: Member, make_user: Member) -> None:
        assert may_use_admin(AccessContext(AnonymousUser())) is False
        assert (
            may_use_admin(AccessContext.for_user(make_user("staff@example.org", is_staff=True)))
            is False
        )
        manager = member(Role.MATERIAL_MANAGER, branch=org.a_mad)
        assert may_use_admin(AccessContext.for_user(manager)) is False
        inactive = make_user("off@example.org", is_superuser=True, is_active=False)
        assert may_use_admin(AccessContext.for_user(inactive)) is False
