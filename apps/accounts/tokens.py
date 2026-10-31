"""Persistent tokens: invitations, email changes, the first administrator (plan §3.6, §5).

A token is 32 random bytes (``secrets``), shown once in an email or on the operator's
terminal. Only its SHA-256 is stored; lookups go by hash and compare it again in constant
time. The raw token never reaches a log, an audit row or the session: the session keeps the
hash between the link and the token-less page that follows it.

This module is an allowlisted home of ``TokenPrincipal`` (``CONSTRUCTOR_MODULES``). Each
principal grants exactly one operation. ``authorize_in_txn`` locks the target and initiator
``User`` rows in id order; the principal's own check then locks the token row and verifies
it under those locks, so offboarding (which locks the same ``User`` rows first) and a second
redemption of the same token always serialize with it.
"""

import hashlib
import hmac
import secrets
from datetime import timedelta
from typing import Literal
from uuid import UUID

from django.db.models import Q

from apps.access.exceptions import TokenRejected
from apps.access.policy import active_platform_operator_exists, valid_now
from apps.access.principals import TokenKind, TokenPrincipal
from apps.access.roles import Role
from apps.accounts.models import EmailChangeRequest, Invitation, User
from apps.core.db import db_now
from apps.core.locking import LockLevel, LockOrder
from apps.organizations.models import Membership

TOKEN_BYTES = 32
# ``secrets.token_urlsafe(32)``: 43 characters of the URL-safe alphabet, no padding.
TOKEN_PATTERN = r"[A-Za-z0-9_-]{43}"  # noqa: S105  # nosec B105 - a pattern, not a secret

INVITATION_LIFETIME = timedelta(hours=72)
EMAIL_CHANGE_LIFETIME = timedelta(hours=24)
EMAIL_CHANGE_COOLING_OFF = timedelta(hours=24)  # Q28

ACCEPT_INVITATION = "account.accept_invitation"
CONFIRM_EMAIL_CHANGE = "account.confirm_email_change"
CANCEL_EMAIL_CHANGE = "account.cancel_email_change"
BOOTSTRAP_ADMIN = "account.bootstrap_admin"

type EmailChangeAction = Literal["confirm", "cancel"]

# The two problems a page can help with; every other one shows the generic error.
SIGN_IN_REQUIRED = "sign in as the invited account"
SIGN_OUT_REQUIRED = "sign out first"


def new_token() -> tuple[str, str]:
    """A fresh token and its hash: ``(raw, sha256 hex)``."""
    raw = secrets.token_urlsafe(TOKEN_BYTES)
    return raw, hash_token(raw)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("ascii", "replace")).hexdigest()


def same_hash(stored: str, candidate: str) -> bool:
    return hmac.compare_digest(stored.encode("ascii"), candidate.encode("ascii"))


# --- shared checks ---------------------------------------------------------------------


def active_group_admin_exists() -> bool:
    """A valid ``GROUP_ADMIN`` membership of an active, not offboarded account."""
    return Membership.objects.filter(
        valid_now(),
        role=Role.GROUP_ADMIN,
        user__is_active=True,
        user__offboarded_at__isnull=True,
    ).exists()


def bootstrap_allowed(*, platform_operator: bool) -> bool:
    """``bootstrap_admin`` runs only while nobody could invite the first administrator."""
    if active_group_admin_exists():
        return False
    return not (platform_operator and active_platform_operator_exists())


def invitation_problem(
    invitation: Invitation, target: User | None, session_user_id: UUID | None
) -> str | None:
    """Why ``invitation`` cannot be accepted by this session now, or ``None``.

    The reasons are for logs and tests only; every page shows the same generic error.
    """
    if not invitation.is_pending:
        return "used or revoked"
    if invitation.expires_at <= db_now():
        return "expired"
    if invitation.kind == Invitation.Kind.NEW_ACCOUNT:
        if session_user_id is not None:
            return SIGN_OUT_REQUIRED
        if User.objects.filter(email=invitation.email).exists():
            return "an account with this email exists"
    else:
        if target is None:
            return "the account no longer exists"
        if target.is_active and target.offboarded_at is None:
            # Accepted only by that very account, signed in; credentials never change.
            if session_user_id != target.pk:
                return SIGN_IN_REQUIRED if session_user_id is None else SIGN_OUT_REQUIRED
        else:
            if session_user_id is not None:
                return SIGN_OUT_REQUIRED
            if target.offboarded_at is not None and target.offboarded_at >= invitation.created_at:
                return "created before the account was offboarded"
    if invitation.bootstrap and not bootstrap_allowed(
        platform_operator=invitation.platform_operator
    ):
        return "an administrator already exists"
    return None


def email_change_problem(change: EmailChangeRequest, action: EmailChangeAction) -> str | None:
    """Why the request cannot be confirmed or cancelled now, or ``None``."""
    if not change.is_open:
        return "applied, cancelled or revoked"
    if action == "cancel":
        return None  # the old address may cancel until the change is applied
    if change.confirmed_at is not None:
        return "already confirmed"
    if change.expires_at <= db_now():
        return "expired"
    user = change.user
    if not user.is_active or user.offboarded_at is not None:
        return "the account is inactive"
    if User.objects.filter(email=change.new_email).exclude(pk=user.pk).exists():
        return "the new address belongs to another account"
    return None


# --- principals ------------------------------------------------------------------------


def _lock_token_row(
    locks: LockOrder, model: type[Invitation | EmailChangeRequest], pk: UUID
) -> None:
    """The token row, ``FOR UPDATE``, after the ``User`` rows (still L0, plan §3.6)."""
    locks.lock(LockLevel.ACCESS, model, [pk])


def invitation_principal(
    token_hash: str, *, session_user_id: UUID | None = None, ip: str | None = None
) -> TokenPrincipal:
    """The principal that accepts the invitation with this token hash.

    Raises ``TokenRejected`` at once for an unknown hash; every other check runs under the
    locks of ``authorize_in_txn``.
    """
    found = (
        Invitation.objects.filter(token_hash=token_hash)
        .values("pk", "email", "target_user_id", "invited_by_id", "token_hash")
        .first()
    )
    if found is None or not same_hash(found["token_hash"], token_hash):
        raise TokenRejected("Unknown invitation")
    invitation_id: UUID = found["pk"]

    def check(target: User | None, locks: LockOrder) -> None:
        _lock_token_row(locks, Invitation, invitation_id)
        invitation = Invitation.objects.get(pk=invitation_id)
        # A new account's target appears only at acceptance: a concurrent one won.
        if invitation.target_user_id != found["target_user_id"]:
            raise TokenRejected("The invitation changed")
        problem = invitation_problem(invitation, target, session_user_id)
        if problem is not None:
            raise TokenRejected(f"The invitation cannot be accepted: {problem}")

    return TokenPrincipal(
        kind=TokenKind.INVITATION,
        operation=ACCEPT_INVITATION,
        target_user_id=found["target_user_id"],
        target_email=found["email"],
        check_token=check,
        token_id=invitation_id,
        initiator_user_id=found["invited_by_id"],
        ip=ip,
    )


def email_change_principal(
    token_hash: str, action: EmailChangeAction, *, ip: str | None = None
) -> TokenPrincipal:
    """The principal that confirms (new address) or cancels (old address) an email change.

    Confirming re-checks the initiator's authority (F71); cancelling is always safe, so it
    does not depend on the initiator.
    """
    field = "token_hash" if action == "confirm" else "cancel_token_hash"
    found = (
        EmailChangeRequest.objects.filter(Q(**{field: token_hash}))
        .values("pk", "user_id", "user__email", "requested_by_id", field)
        .first()
    )
    if found is None or not same_hash(found[field], token_hash):
        raise TokenRejected("Unknown email change")
    change_id: UUID = found["pk"]

    def check(target: User | None, locks: LockOrder) -> None:
        _lock_token_row(locks, EmailChangeRequest, change_id)
        change = EmailChangeRequest.objects.select_related("user").get(pk=change_id)
        problem = email_change_problem(change, action)
        if problem is not None:
            raise TokenRejected(f"The email change cannot be {action}ed: {problem}")

    return TokenPrincipal(
        kind=TokenKind.EMAIL_CHANGE,
        operation=CONFIRM_EMAIL_CHANGE if action == "confirm" else CANCEL_EMAIL_CHANGE,
        target_user_id=found["user_id"],
        target_email=found["user__email"],
        check_token=check,
        token_id=change_id,
        initiator_user_id=found["requested_by_id"] if action == "confirm" else None,
        ip=ip,
    )


def bootstrap_principal(email: str, *, platform_operator: bool) -> TokenPrincipal:
    """The principal of ``manage bootstrap_admin`` (F76): valid while no administrator exists."""

    def check(target: User | None, locks: LockOrder) -> None:
        if not bootstrap_allowed(platform_operator=platform_operator):
            raise TokenRejected("An administrator already exists")

    return TokenPrincipal(
        kind=TokenKind.BOOTSTRAP,
        operation=BOOTSTRAP_ADMIN,
        target_user_id=None,
        target_email=email,
        check_token=check,
    )
