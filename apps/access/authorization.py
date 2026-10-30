"""Interim authorization for the organization and project services (M2.2, plan §3.6).

M2.3 replaces this module's policy with the access engine (``ROLE_CAPABILITIES``, scopes
S1-S10, every principal type, session epoch and MFA checks). Until then it fails closed:

- ``authorize_in_txn`` takes the L0 locks and admits only an active, not offboarded user
  who holds a valid ``GROUP_ADMIN`` membership at the database clock, and only for
  ``org.manage`` and ``project.manage``. ``MATERIAL_MANAGER``'s ``project.manage`` within
  its scope arrives with M2.3; every other capability is refused.
- ``is_superuser`` grants no business capability. It only lets a platform operator reach
  the admin and act on staff accounts (plan §3.4); those checks live here, in
  ``apps/access``, as CLAUDE.md requires.
"""

from typing import Any
from uuid import UUID

from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.db.models.functions import Now

from apps.access.generations import lock_access_state, lock_users
from apps.access.principals import Principal
from apps.access.roles import Role
from apps.accounts.models import User
from apps.core.locking import LockOrder
from apps.organizations.models import Membership

ORG_MANAGE = "org.manage"
PROJECT_MANAGE = "project.manage"
GROUP_ADMIN_CAPABILITIES = frozenset({ORG_MANAGE, PROJECT_MANAGE})


class AccessDenied(PermissionDenied):
    """The principal may not perform this operation (403)."""


def valid_now() -> Q:
    """S8: not revoked and ``valid_from ≤ now < valid_until`` at the database clock."""
    return Q(revoked_at__isnull=True, valid_from__lte=Now()) & (
        Q(valid_until__isnull=True) | Q(valid_until__gt=Now())
    )


def holds_group_admin(user_id: UUID) -> bool:
    """A valid ``GROUP_ADMIN`` membership (it has no branch or company, so S9 cannot apply)."""
    return Membership.objects.filter(valid_now(), user_id=user_id, role=Role.GROUP_ADMIN).exists()


def authorize_in_txn(principal: Principal, capability: str, *, locks: LockOrder) -> User:
    """Lock L0 and check ``capability`` for ``principal``; return the actor's ``User`` row.

    Call it first in the service transaction, after the L0 rows that the service changes
    (those are locked ``FOR UPDATE`` before this ``FOR SHARE``). Raises ``AccessDenied``.
    """
    lock_access_state(locks)
    lock_users(locks, [principal.user_id])
    actor = User.objects.filter(
        pk=principal.user_id, is_active=True, offboarded_at__isnull=True
    ).first()
    if (
        actor is None
        or capability not in GROUP_ADMIN_CAPABILITIES
        or not holds_group_admin(actor.pk)
    ):
        raise AccessDenied(capability)
    return actor


def require_may_manage_account(actor: User, target: User) -> None:
    """Only a superuser acts on a staff or superuser account (plan §3.4)."""
    if (target.is_staff or target.is_superuser) and not actor.is_superuser:
        raise AccessDenied("Only a platform operator can manage a staff account")


def may_use_admin(user: Any) -> bool:
    """Superusers and group administrators reach the admin (plan §5, "Django Admin").

    The admin site adds the session-MFA requirement on top.
    """
    if not getattr(user, "is_active", False) or not user.is_authenticated:
        return False
    return bool(user.is_superuser) or holds_group_admin(user.pk)


def may_manage_organization(user: Any) -> bool:
    """Whether the admin offers master-data edits; the service checks again under lock."""
    return bool(
        getattr(user, "is_active", False)
        and user.is_authenticated
        and user.offboarded_at is None
        and holds_group_admin(user.pk)
    )
