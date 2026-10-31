"""Shared access rules (apps.access.policy): privilege, MFA, staff accounts, self-approval."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from django.contrib.auth.models import AnonymousUser
from django.utils import timezone

from apps.access.exceptions import AccessDenied
from apps.access.policy import (
    holds_privileged_role,
    is_platform_operator,
    is_privileged,
    mfa_satisfied,
    require_may_manage_account,
    require_not_self_approval,
)
from apps.access.roles import Role
from apps.accounts.models import User
from apps.organizations.models import Branch
from tests.support import grant

pytestmark = pytest.mark.django_db

HOUR = timedelta(hours=1)


class TestPrivilege:
    def test_platform_operators_and_privileged_roles(
        self, make_user: Callable[..., User], branch: Branch
    ) -> None:
        assert is_privileged(make_user("ops@example.org", is_staff=True))
        assert is_privileged(make_user("root@example.org", is_superuser=True))
        for role, at in ((Role.GROUP_ADMIN, None), (Role.MATERIAL_MANAGER, branch)):
            user = make_user(f"{role.lower()}@example.org")
            grant(user, role, branch=at)
            assert is_privileged(user) and holds_privileged_role(user.pk)

    def test_not_ordinary_or_lapsed_roles(
        self, make_user: Callable[..., User], branch: Branch
    ) -> None:
        technician = make_user()
        grant(technician, Role.TECHNICIAN, branch=branch)
        former = make_user("former@example.org")
        grant(
            former,
            Role.MATERIAL_MANAGER,
            branch=branch,
            valid_from=timezone.now() - 2 * HOUR,
            valid_until=timezone.now() - HOUR,
        )
        assert not is_privileged(technician)
        assert not is_privileged(former)
        assert not is_privileged(AnonymousUser())

    def test_a_privileged_role_on_an_inactive_branch_still_counts(
        self, make_user: Callable[..., User], branch: Branch
    ) -> None:
        """Strict on purpose: MFA and short sessions never hurt."""
        manager = make_user("mm@example.org")
        grant(manager, Role.MATERIAL_MANAGER, branch=branch)
        Branch.objects.filter(pk=branch.pk).update(is_active=False)
        assert is_privileged(manager)

    def test_platform_operators_are_active_superusers(self, make_user: Callable[..., User]) -> None:
        assert is_platform_operator(make_user("root@example.org", is_superuser=True))
        assert not is_platform_operator(make_user("ops@example.org", is_staff=True))
        assert not is_platform_operator(
            make_user("off@example.org", is_superuser=True, is_active=False)
        )
        assert not is_platform_operator(AnonymousUser())
        assert not is_platform_operator(None)


class TestMfaSatisfied:
    ELEVATED = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)

    def test_needs_a_completed_mfa(self) -> None:
        assert not mfa_satisfied(None, None)
        assert mfa_satisfied(1.0, None)

    def test_after_the_last_elevation(self) -> None:
        at = self.ELEVATED.timestamp()
        assert not mfa_satisfied(at - 1, self.ELEVATED)
        assert mfa_satisfied(at, self.ELEVATED)
        assert mfa_satisfied(at + 1, self.ELEVATED)


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


class TestSelfApproval:
    def test_forbidden_unless_the_branch_allows_it(
        self, make_user: Callable[..., User], branch: Branch
    ) -> None:
        reviewer, requester = make_user("mm@example.org"), make_user()
        require_not_self_approval(reviewer.pk, requester.pk, branch)
        with pytest.raises(AccessDenied, match="Self-approval"):
            require_not_self_approval(reviewer.pk, reviewer.pk, branch)
        branch.allow_self_approval = True
        require_not_self_approval(reviewer.pk, reviewer.pk, branch)
