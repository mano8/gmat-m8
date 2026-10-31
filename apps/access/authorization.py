"""``authorize_in_txn``: the full access check inside every write (plan §3.6, S10).

Every mutating service calls it first in its transaction, after the L0 rows the service
itself changes (those are locked ``FOR UPDATE`` before this ``FOR SHARE``). There is no
shortcut when nothing seems to have changed: each call runs the whole check of its
principal type.

**Session principal** (§3.6 steps 1-6):

1. lock ``AccessState`` and the actor's ``User`` row ``FOR SHARE``;
2. the user is active and not offboarded, and the session's epoch equals the user's
   ``session_epoch`` (``StaleSession`` otherwise: a password, email or MFA change or a
   deactivation since the page load);
3. reload the memberships and assignments and evaluate them at the transaction's clock
   (S8, S9), so an expiry that bumped no counter is caught;
4. every object of this operation is in the capability's scope (S6: one membership's
   grant has to cover it); with ``read``, an object the user cannot even see is
   ``NotInScope`` (404) rather than ``AccessDenied`` (403, S7);
5. when only privileged grants cover the operation, the session completed MFA after
   ``privilege_elevated_at`` (``MFARequired``);
6. any denial raises, and the service's transaction rolls back.

**Operator principal**: steps 1-4 for the command's one capability; no session exists, so
neither the epoch nor MFA applies (the audit shows ``via=cli:<command>``).

**Token principal**: exactly its one operation. Locks the target and initiator ``User``
rows in id order, lets the token's own check lock and verify the token row, then requires
that the initiator still holds ``org.manage`` (a superuser for a staff target, F71).

**System principal**: only its closed capability set; never a business capability.
"""

from typing import cast, overload
from uuid import UUID

from django.db import connections
from django.db.models import Model

from apps.access.context import AccessContext
from apps.access.exceptions import (
    AccessDenied,
    MFARequired,
    NotInScope,
    StaleSession,
    TokenRejected,
)
from apps.access.generations import lock_access_state, lock_users
from apps.access.policy import mfa_satisfied, require_may_manage_account
from apps.access.principals import (
    AnyPrincipal,
    OperatorPrincipal,
    SessionPrincipal,
    SystemPrincipal,
    TokenPrincipal,
)
from apps.access.roles import Capability
from apps.access.scopes import Scope, object_in_scope
from apps.accounts.models import User
from apps.core.locking import LockMode, LockOrder, LockOutsideTransaction


@overload
def authorize_in_txn(
    principal: SessionPrincipal | OperatorPrincipal,
    capability: str,
    *objects: Model,
    locks: LockOrder,
    read: str | None = None,
) -> User: ...


@overload
def authorize_in_txn(
    principal: TokenPrincipal, capability: str, *, locks: LockOrder
) -> User | None: ...


@overload
def authorize_in_txn(principal: SystemPrincipal, capability: str, *, locks: LockOrder) -> None: ...


def authorize_in_txn(
    principal: AnyPrincipal,
    capability: str,
    *objects: Model,
    locks: LockOrder,
    read: str | None = None,
) -> User | None:
    """Check ``capability`` (on ``objects``) for ``principal``; return the acting user.

    Session and operator principals get their ``User`` row; a token principal its target
    (``None`` before a new account exists); the system principal ``None``.
    """
    if not connections[locks.using].in_atomic_block:
        raise LockOutsideTransaction("authorize_in_txn runs inside the service transaction")
    if isinstance(principal, SessionPrincipal | OperatorPrincipal):
        return _authorize_user(principal, capability, objects, read, locks)
    if objects or read is not None:
        raise TypeError(f"{type(principal).__name__} is not authorized on objects")
    if isinstance(principal, TokenPrincipal):
        return _authorize_token(principal, capability, locks)
    if not principal.may(capability):
        raise AccessDenied(capability)
    return None


def _covers(scope: Scope, obj: Model | None) -> bool:
    return bool(scope) if obj is None else object_in_scope(obj, scope)


def _authorize_user(
    principal: SessionPrincipal | OperatorPrincipal,
    capability: str,
    objects: tuple[Model, ...],
    read: str | None,
    locks: LockOrder,
) -> User:
    lock_access_state(locks)
    lock_users(locks, [principal.user_id])
    user = User.objects.filter(pk=principal.user_id).first()
    if user is None or not user.is_active or user.offboarded_at is not None:
        raise AccessDenied(capability)
    if isinstance(principal, SessionPrincipal):
        if principal.session_epoch != user.session_epoch:
            raise StaleSession("The session predates a change to this account")
    elif capability != principal.capability:
        raise AccessDenied(f"{principal.via} runs with {principal.capability} only")
    ctx = AccessContext.for_user(user)
    if read is not None:
        visible = ctx.scope_for(read)
        if not all(object_in_scope(obj, visible) for obj in objects):
            raise NotInScope(read)
    scope = ctx.scope_for(capability)
    ordinary, privileged = scope.only(privileged=False), scope.only(privileged=True)
    needs_mfa = False
    for obj in objects or (None,):
        if _covers(ordinary, obj):
            continue
        if not _covers(privileged, obj):
            raise AccessDenied(capability)
        needs_mfa = True
    if (
        needs_mfa
        and isinstance(principal, SessionPrincipal)
        and not mfa_satisfied(principal.mfa_at, user.privilege_elevated_at)
    ):
        raise MFARequired(capability)
    return user


def _authorize_token(principal: TokenPrincipal, capability: str, locks: LockOrder) -> User | None:
    if capability != principal.operation:
        raise AccessDenied(f"This token grants {principal.operation} only")
    lock_access_state(locks)
    user_ids = {principal.target_user_id, principal.initiator_user_id} - {None}
    lock_users(locks, cast(set[UUID], user_ids), mode=LockMode.NO_KEY_UPDATE)
    target = None
    if principal.target_user_id is not None:
        target = User.objects.filter(pk=principal.target_user_id).first()
        if target is None:
            raise TokenRejected("The token's account no longer exists")
    principal.check_token(target, locks)
    if principal.initiator_user_id is not None:
        initiator = User.objects.filter(pk=principal.initiator_user_id).first()
        if initiator is None or not AccessContext.for_user(initiator).has(Capability.ORG_MANAGE):
            raise TokenRejected("The token's issuer no longer holds that authority")
        if target is not None:
            require_may_manage_account(initiator, target)
    return target


def may_use_admin(ctx: AccessContext) -> bool:
    """Platform operators and group administrators reach the admin (plan §5).

    The admin site adds the session-MFA requirement on top.
    """
    return ctx.is_platform_operator or ctx.has(Capability.ORG_MANAGE)
