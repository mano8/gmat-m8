"""An account's second factors and sessions: the MFA reset, the encryption key, own sessions."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from allauth.mfa.models import Authenticator
from allauth.usersessions.models import UserSession
from cryptography.fernet import InvalidToken
from django.contrib.sessions.backends.db import SessionStore
from django.core.exceptions import ValidationError
from django.template.loader import render_to_string
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

from apps.access.authorization import authorize_in_txn
from apps.access.principals import Principal, SessionPrincipal
from apps.access.roles import OWN_ACCOUNT, Capability
from apps.accounts.adapters import MFAAdapter
from apps.accounts.mfa import reset_mfa
from apps.accounts.models import User
from apps.accounts.services.common import MFA_RESET_MAIL, _lock_and_authorize, _subject
from apps.audit.recorder import record
from apps.core import events
from apps.core.locking import LockOrder, transactional
from apps.notifications.eligibility import ACCOUNT_ACTIVE
from apps.notifications.outbox import queue_security_email

# Where allauth keeps what MFA_ENCRYPTION_KEYS encrypts: a TOTP secret, a recovery-code seed.
MFA_SECRET_FIELDS = frozenset({"secret", "seed"})


@transactional
def reset_user_mfa(principal: Principal, user_id: UUID, *, reason: str) -> User:
    """Remove every second factor of an account that lost them (plan §5, M8.3).

    An administrator's action (``org.manage``; a superuser for a staff account), audited with
    its reason. Every session of the account ends, and its next sign-in enrolls a new factor
    where its roles need one. The account's owner is told, as for a change they make
    themselves. Nobody resets their own factors this way.
    """
    actor = _lock_and_authorize(principal, user_id)
    if actor.pk == user_id:
        raise ValidationError(_("You cannot reset your own second factors."), code="self")
    reason = reason.strip()
    if not reason:
        raise ValidationError({"reason": ValidationError(_("Give the reason."), code="required")})
    user = User.objects.get(pk=user_id)
    removed = sorted(Authenticator.objects.filter(user=user).values_list("type", flat=True))
    reset_mfa(user)
    queue_security_email(
        address=user.email,
        target=user,
        rule=ACCOUNT_ACTIVE,
        subject=_subject(gettext("Your second factors were removed")),
        body=render_to_string("accounts/email/mfa_reset_notice.txt"),
        source_ref=MFA_RESET_MAIL,
    )
    record(
        principal.audit_identity,
        "user.mfa_reset",
        obj=user,
        data={"factors_removed": removed, "reason": reason},
    )
    events.publish("user.mfa_reset", user_id=user.pk)
    return user


# --- the MFA encryption key (M8.5) -----------------------------------------------------


@dataclass(frozen=True)
class MfaReencryption:
    """What a run did: secrets moved to the newest key, already there, and the accounts no
    listed key opens (their owners cannot pass MFA until ``reset_user_mfa``)."""

    moved: int
    already: int
    unreadable: tuple[UUID, ...]


def _reencrypted(data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """``data`` with its secret under the newest key, and whether that changed anything.
    Passkeys hold no secret of ours."""
    adapter = MFAAdapter()
    for field in MFA_SECRET_FIELDS & set(data):
        if adapter.is_under_newest_key(data[field]):
            return data, False
        return {**data, field: adapter.reencrypt(data[field])}, True
    return data, False


@transactional
def reencrypt_mfa_secrets(principal: Principal) -> MfaReencryption:
    """Move every stored second-factor secret to the first of ``MFA_ENCRYPTION_KEYS``, so an old
    key can leave the list. Idempotent: run it again, and ``moved`` 0 says none is left behind
    (a sign-in in progress may have written an old value back)."""
    authorize_in_txn(principal, Capability.ORG_MANAGE, locks=LockOrder())
    moved, already = 0, 0
    unreadable: list[UUID] = []
    for authenticator in Authenticator.objects.select_for_update().order_by("pk"):
        try:
            data, changed = _reencrypted(authenticator.data)
        except InvalidToken:
            unreadable.append(authenticator.user_id)
            continue
        moved += changed
        already += not changed
        if changed:
            authenticator.data = data
            authenticator.save(update_fields=["data"])
    result = MfaReencryption(moved, already, tuple(sorted(set(unreadable), key=str)))
    record(
        principal.audit_identity,
        "mfa.secrets_reencrypted",
        data={"moved": moved, "already": already, "unreadable": len(result.unreadable)},
    )
    return result


# --- one's own sessions (M8.3, X5) ------------------------------------------------------


def _end_sessions(principal: Principal, user: User, rows: Sequence[UserSession]) -> int:
    """End these sessions now and audit it. A restore ends every session, so unlike what takes
    a credential away, this needs no replay (``apps.recovery.replay``)."""
    store = SessionStore()
    for row in rows:
        store.delete(row.session_key)
        row.delete()
    if rows:
        record(principal.audit_identity, "user.sessions_ended", obj=user, data={"count": len(rows)})
    return len(rows)


@transactional
def end_own_session(principal: SessionPrincipal, session_id: int, *, current_key: str) -> int:
    """End one other session of the signed-in user; how many ended (0: it had ended already,
    or it is not theirs, which looks the same)."""
    user = authorize_in_txn(principal, OWN_ACCOUNT, locks=LockOrder())
    rows = list(UserSession.objects.filter(user=user, pk=session_id))
    if any(row.session_key == current_key for row in rows):
        raise ValidationError(_("Sign out to end this session."), code="current")
    return _end_sessions(principal, user, rows)


@transactional
def end_other_sessions(principal: SessionPrincipal, *, current_key: str) -> int:
    """End every session of the signed-in user but this one; how many ended."""
    user = authorize_in_txn(principal, OWN_ACCOUNT, locks=LockOrder())
    rows = list(UserSession.objects.filter(user=user).exclude(session_key=current_key))
    return _end_sessions(principal, user, rows)
