"""What the account services share: the security-email rules, the locks, the wording of mail."""

from typing import Any
from uuid import UUID

from django.conf import settings

from apps.access.authorization import authorize_in_txn
from apps.access.exceptions import TokenRejected
from apps.access.generations import lock_access_state, lock_users
from apps.access.policy import require_may_manage_account
from apps.access.principals import Principal, TokenPrincipal
from apps.access.roles import Capability
from apps.accounts.models import User
from apps.core.locking import LockMode, LockOrder

# Security-email rules (registered in ``apps.accounts.eligibility``).
INVITATION_PENDING = "invitation.pending"
EMAIL_CHANGE_CONFIRMABLE = "email_change.confirmable"
EMAIL_CHANGE_CANCELLABLE = "email_change.cancellable"
# Their ``source_ref``: which message about the invitation or change a row is (M7c.1 badges).
INVITATION_MAIL = "invitation"
EMAIL_CHANGE_CONFIRM_MAIL = "email_change.confirm"
EMAIL_CHANGE_NOTICE_MAIL = "email_change.cancel"
MFA_RESET_MAIL = "mfa_reset"

REASON_RESENT = "resent"
REASON_REPLACED = "replaced"
REASON_OFFBOARDED = "offboarded"


# --- emails ----------------------------------------------------------------------------


def _absolute(path: str) -> str:
    return f"{str(settings.PUBLIC_BASE_URL).rstrip('/')}{path}"


def _subject(text: str) -> str:
    return f"{settings.ACCOUNT_EMAIL_SUBJECT_PREFIX}{text}"


def _hours(lifetime: Any) -> int:
    return int(lifetime.total_seconds() // 3600)


# --- locks -----------------------------------------------------------------------------


def _lock_and_authorize(principal: Principal, target_id: UUID | None) -> User:
    """L0 for a change to ``target_id``'s account (or for a new one), then ``org.manage``."""
    locks = LockOrder()
    lock_access_state(locks)
    lock_users(
        locks,
        {principal.user_id} | ({target_id} if target_id else set()),
        mode=LockMode.NO_KEY_UPDATE,
    )
    actor = authorize_in_txn(principal, Capability.ORG_MANAGE, locks=locks)
    if target_id is not None:
        require_may_manage_account(actor, User.objects.get(pk=target_id))
    return actor


def _token_row(principal: TokenPrincipal) -> UUID:
    """The token row a persistent-token principal was built for."""
    if principal.token_id is None:
        raise TokenRejected("This principal carries no token row")
    return principal.token_id


def _normalize_email(email: str) -> str:
    return email.strip().lower()
