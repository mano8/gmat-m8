"""Rules shared by the access engine and the account code (plan §3.4 to §3.6, §5).

These are the only places outside the engine that read ``is_staff`` / ``is_superuser``:
platform operators have short sessions and mandatory MFA, reach the admin, and alone act
on staff accounts. They never grant a business capability (CLAUDE.md, "Access").
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from django.db.models import Q
from django.db.models.functions import Now

from apps.access.exceptions import AccessDenied
from apps.access.roles import PRIVILEGED_ROLES
from apps.organizations.models import Branch, Membership


def valid_now() -> Q:
    """S8: not revoked and ``valid_from ≤ now < valid_until`` at the database clock.

    ``Now()`` is the statement's time on PostgreSQL: the clock inside the transaction.
    """
    return Q(revoked_at__isnull=True, valid_from__lte=Now()) & (
        Q(valid_until__isnull=True) | Q(valid_until__gt=Now())
    )


def holds_privileged_role(user_id: UUID) -> bool:
    """A valid membership of a role that requires MFA (``GROUP_ADMIN``, ``MATERIAL_MANAGER``).

    Organization state (S9) is ignored on purpose: an MFA requirement or a shorter session
    for a role on an inactive branch only ever errs on the strict side.
    """
    memberships = Membership.objects.filter(valid_now(), user_id=user_id)
    return memberships.filter(role__in=PRIVILEGED_ROLES).exists()


def is_privileged(user: Any) -> bool:
    """Accounts with short sessions (Q12) and mandatory MFA (plan §5).

    Platform operators (staff, superusers) and holders of a privileged role.
    """
    if not getattr(user, "is_authenticated", False):
        return False
    return bool(user.is_staff or user.is_superuser) or holds_privileged_role(user.pk)


def mfa_satisfied(mfa_at: float | None, privilege_elevated_at: datetime | None) -> bool:
    """Whether "MFA required" holds: MFA completed after the last elevation (plan §5).

    ``mfa_at`` is when the session last completed MFA (seconds since the epoch).
    """
    if mfa_at is None:
        return False
    return privilege_elevated_at is None or mfa_at >= privilege_elevated_at.timestamp()


def is_platform_operator(user: Any) -> bool:
    """An active superuser: reaches the admin (read-only without ``org.manage``)."""
    return bool(getattr(user, "is_authenticated", False) and user.is_active and user.is_superuser)


def require_may_manage_account(actor: Any, target: Any) -> None:
    """Only a superuser acts on a staff or superuser account (plan §3.4)."""
    if (target.is_staff or target.is_superuser) and not actor.is_superuser:
        raise AccessDenied("Only a platform operator can manage a staff account")


def require_not_self_approval(actor_id: UUID, requester_id: UUID, branch: Branch) -> None:
    """Nobody approves their own request unless ``branch.allow_self_approval`` (plan §3.4).

    ``branch`` is the branch that reviews the request (its procurement branch, M4.2).
    """
    if actor_id == requester_id and not branch.allow_self_approval:
        raise AccessDenied("Self-approval is not allowed on this branch")
