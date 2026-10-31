"""Invitation races (M2.6 done-when): two redemptions of one token, offboarding vs acceptance.

Real threads with their own database connections. Acceptance locks the target and inviter
``User`` rows (id order) before the invitation row; offboarding locks the same ``User`` rows
first, so the two serialize whichever starts first.
"""

import time
from collections.abc import Callable
from typing import Any

import pytest

from apps.access.exceptions import TokenRejected
from apps.access.principals import SessionPrincipal
from apps.access.roles import Role
from apps.accounts import services, tokens
from apps.accounts.models import Invitation, User
from apps.accounts.services import MembershipGrant
from apps.organizations.models import Branch, Membership
from tests.integration.conftest import run_concurrently
from tests.support import grant
from tests.tokens import last_token

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]

PASSWORDS = ["first-password-1234", "second-password-1234"]
HOLD = 0.4  # seconds the first transaction keeps its locks


def slow(monkeypatch: pytest.MonkeyPatch, target: str) -> None:
    """Make ``target`` (a function of ``apps.accounts.services``) hold its locks a while."""
    original = getattr(services, target)

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        time.sleep(HOLD)
        return result

    monkeypatch.setattr(services, target, wrapper)


def test_two_concurrent_redemptions_one_succeeds(
    admin_principal: SessionPrincipal, branch: Branch, monkeypatch: pytest.MonkeyPatch
) -> None:
    services.create_invitation(
        admin_principal,
        email="new@example.org",
        memberships=[MembershipGrant(role=Role.TECHNICIAN, branch_id=branch.pk)],
    )
    digest = tokens.hash_token(last_token("new@example.org"))
    slow(monkeypatch, "_verified_email")

    def redeem(index: int) -> str:
        principal = tokens.invitation_principal(digest)
        services.accept_invitation(principal, password=PASSWORDS[index])
        return PASSWORDS[index]

    results = run_concurrently(2, redeem)
    winners = [result for result in results if isinstance(result, str)]
    losers = [result for result in results if isinstance(result, TokenRejected)]
    assert len(winners) == 1 and len(losers) == 1, results
    user = User.objects.get(email="new@example.org")
    assert user.check_password(winners[0])
    assert Membership.objects.filter(user=user).count() == 1
    assert Invitation.objects.get().accepted_at is not None


@pytest.mark.parametrize("first", ["accept", "offboard"])
def test_offboarding_racing_an_acceptance_leaves_the_user_out(
    first: str,
    admin_principal: SessionPrincipal,
    make_user: Callable[..., User],
    branch: Branch,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = make_user("old@example.org")
    grant(user, Role.TECHNICIAN, branch=branch)
    services.create_invitation(
        admin_principal,
        email=user.email,
        memberships=[MembershipGrant(role=Role.WAREHOUSE_OPERATOR, branch_id=branch.pk)],
    )
    digest = tokens.hash_token(last_token(user.email))
    slow(monkeypatch, "_grant_on_acceptance" if first == "accept" else "revoke_pending_tokens")

    def accept() -> str:
        principal = tokens.invitation_principal(digest, session_user_id=user.pk)
        services.accept_invitation(principal)
        return "accepted"

    def offboard() -> str:
        services.offboard_user(admin_principal, user.pk)
        return "offboarded"

    order = [accept, offboard] if first == "accept" else [offboard, accept]

    def work(index: int) -> str:
        if index == 1:
            time.sleep(HOLD / 3)  # the other one already holds the User rows
        return order[index]()

    results = run_concurrently(2, work)
    user.refresh_from_db()
    assert not user.is_active and user.offboarded_at is not None
    assert not Membership.objects.filter(user=user, revoked_at__isnull=True).exists()
    assert "offboarded" in results
    if first == "offboard":
        assert isinstance(results[1], TokenRejected), results
        assert Invitation.objects.get().revoked_at is not None
    else:
        assert results == ["accepted", "offboarded"]
