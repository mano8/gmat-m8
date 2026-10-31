"""Fixtures of the invitation, email-change and offboarding tests."""

from collections.abc import Callable
from typing import Any

import pytest

from apps.access.principals import SessionPrincipal
from apps.access.roles import Role
from apps.accounts import services
from apps.accounts.models import Invitation
from apps.accounts.services import AssignmentGrant, MembershipGrant
from apps.organizations.models import Branch


@pytest.fixture
def invite(admin_principal: SessionPrincipal, branch: Branch) -> Callable[..., Invitation]:
    """``invite(email, role=TECHNICIAN, …)``: an invitation from the group administrator."""

    def make(
        email: str = "new@example.org",
        role: Role = Role.TECHNICIAN,
        *,
        principal: SessionPrincipal | None = None,
        assignments: tuple[AssignmentGrant, ...] = (),
        **kwargs: Any,
    ) -> Invitation:
        grant_kwargs: dict[str, Any] = {}
        if role in {
            Role.MATERIAL_MANAGER,
            Role.WAREHOUSE_OPERATOR,
            Role.TECHNICIAN,
            Role.SITE_MANAGER,
        }:
            grant_kwargs["branch_id"] = kwargs.pop("branch_id", branch.pk)
        if role is Role.PARTNER_REQUESTER:
            grant_kwargs["company_id"] = kwargs["employer_company_id"]
        return services.create_invitation(
            principal or admin_principal,
            email=email,
            memberships=[MembershipGrant(role=role, **grant_kwargs)],
            assignments=assignments,
            **kwargs,
        )

    return make
