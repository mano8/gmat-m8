"""Interim authorization of the M2.2 services (apps.access.authorization)."""

from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.test import RequestFactory
from django.utils import timezone

from apps.access.authorization import (
    ORG_MANAGE,
    PROJECT_MANAGE,
    AccessDenied,
    authorize_in_txn,
    holds_group_admin,
    may_manage_organization,
    may_use_admin,
    require_may_manage_account,
)
from apps.access.principals import SessionPrincipal
from apps.access.roles import Role
from apps.accounts.models import User
from apps.audit.recorder import AuditIdentity
from apps.core.db import db_now
from apps.core.locking import LockOrder
from apps.organizations.models import Branch
from tests.support import grant, principal_for

pytestmark = pytest.mark.django_db

HOUR = timedelta(hours=1)


def authorize(user: User, capability: str = ORG_MANAGE) -> User:
    with transaction.atomic():
        return authorize_in_txn(principal_for(user), capability, locks=LockOrder())


class TestAuthorizeInTxn:
    @pytest.mark.parametrize("capability", [ORG_MANAGE, PROJECT_MANAGE])
    def test_group_admin_holds_the_interim_capabilities(
        self, group_admin: User, capability: str
    ) -> None:
        assert authorize(group_admin, capability) == group_admin

    def test_other_capabilities_are_refused(self, group_admin: User) -> None:
        with pytest.raises(AccessDenied):
            authorize(group_admin, "price.view")

    def test_other_roles_are_refused_until_m2_3(
        self, make_user: Callable[..., User], branch: Branch
    ) -> None:
        manager = make_user("mm@example.org")
        grant(manager, Role.MATERIAL_MANAGER, branch=branch)
        with pytest.raises(AccessDenied):
            authorize(manager, PROJECT_MANAGE)

    def test_superuser_alone_has_no_business_capability(
        self, make_user: Callable[..., User]
    ) -> None:
        operator = make_user("ops@example.org", is_staff=True, is_superuser=True)
        with pytest.raises(AccessDenied):
            authorize(operator)

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
        self, make_user: Callable[..., User], window: dict[str, Any]
    ) -> None:
        user = make_user("former@example.org")
        grant(user, Role.GROUP_ADMIN, **window)
        with pytest.raises(AccessDenied):
            authorize(user)

    def test_validity_is_half_open(self, make_user: Callable[..., User]) -> None:
        user = make_user("edge@example.org")
        with transaction.atomic():
            now = db_now()
            grant(user, Role.GROUP_ADMIN, valid_from=now - HOUR, valid_until=now)
            with pytest.raises(AccessDenied):
                authorize_in_txn(principal_for(user), ORG_MANAGE, locks=LockOrder())

    @pytest.mark.parametrize(
        "change",
        [{"is_active": False}, {"offboarded_at": timezone.now()}],
        ids=["inactive", "offboarded"],
    )
    def test_inactive_or_offboarded_users_are_refused(
        self, group_admin: User, change: dict[str, Any]
    ) -> None:
        User.objects.filter(pk=group_admin.pk).update(**change)
        with pytest.raises(AccessDenied):
            authorize(group_admin)

    def test_unknown_principals_are_refused(self) -> None:
        ghost = SessionPrincipal(user_id=User(email="ghost@example.org").pk, email="ghost")
        with transaction.atomic(), pytest.raises(AccessDenied):
            authorize_in_txn(ghost, ORG_MANAGE, locks=LockOrder())

    def test_access_denied_is_a_403(self) -> None:
        assert issubclass(AccessDenied, PermissionDenied)


class TestStaffAccounts:
    def test_a_group_admin_cannot_manage_staff(
        self, group_admin: User, make_user: Callable[..., User]
    ) -> None:
        for flags in ({"is_staff": True}, {"is_superuser": True}):
            target = make_user(f"{next(iter(flags))}@example.org", **flags)
            with pytest.raises(AccessDenied):
                require_may_manage_account(group_admin, target)

    def test_a_superuser_can(self, make_user: Callable[..., User]) -> None:
        operator = make_user("ops@example.org", is_superuser=True)
        require_may_manage_account(operator, make_user("staff@example.org", is_staff=True))

    def test_ordinary_accounts_need_no_superuser(
        self, group_admin: User, make_user: Callable[..., User]
    ) -> None:
        require_may_manage_account(group_admin, make_user())


class TestAdminAccess:
    def test_superusers_and_group_admins_reach_the_admin(
        self, group_admin: User, make_user: Callable[..., User]
    ) -> None:
        assert may_use_admin(group_admin) is True
        assert may_use_admin(make_user("ops@example.org", is_superuser=True)) is True

    def test_everyone_else_does_not(self, make_user: Callable[..., User]) -> None:
        assert may_use_admin(AnonymousUser()) is False
        assert may_use_admin(make_user()) is False
        assert may_use_admin(make_user("staff@example.org", is_staff=True)) is False
        assert (
            may_use_admin(make_user("off@example.org", is_superuser=True, is_active=False)) is False
        )

    def test_only_group_admins_edit_master_data(
        self, group_admin: User, make_user: Callable[..., User]
    ) -> None:
        assert may_manage_organization(group_admin) is True
        assert may_manage_organization(make_user("ops@example.org", is_superuser=True)) is False
        assert may_manage_organization(AnonymousUser()) is False
        User.objects.filter(pk=group_admin.pk).update(offboarded_at=timezone.now())
        group_admin.refresh_from_db()
        assert may_manage_organization(group_admin) is False

    def test_holds_group_admin_reads_the_database(self, group_admin: User) -> None:
        assert holds_group_admin(group_admin.pk) is True


class TestSessionPrincipal:
    def test_built_from_the_request(self, group_admin: User) -> None:
        request = RequestFactory().get("/", REMOTE_ADDR="10.1.2.3")
        request.user = group_admin
        principal = SessionPrincipal.from_request(request)
        assert principal == SessionPrincipal(group_admin.pk, group_admin.email, "10.1.2.3")
        assert principal.audit_identity == AuditIdentity(
            label=group_admin.email, user_id=group_admin.pk, ip="10.1.2.3"
        )

    def test_needs_an_authenticated_user(self) -> None:
        request = RequestFactory().get("/")
        request.user = AnonymousUser()
        with pytest.raises(ValueError, match="authenticated"):
            SessionPrincipal.from_request(request)
