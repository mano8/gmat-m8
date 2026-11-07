"""Account services: invitations, offboarding, email changes, the first administrator (plan §5).

Same write order as ``apps.organizations.services``: L0 locks (``AccessState`` ``FOR SHARE``,
then the target and actor ``User`` rows ``FOR NO KEY UPDATE`` in id order, then token rows),
``authorize_in_txn``, the idempotency record for a create that carries a key, validation, the
write, ``audit.record`` and ``events.publish``. Emails are queued in the same transaction as
sensitive security messages; their links carry the raw token, which nothing else keeps.

- Administrators (``org.manage``; a superuser for a staff account) create, resend and revoke
  invitations, offboard accounts and request or revoke email changes.
- Token principals (``apps.accounts.tokens``) accept an invitation, or confirm or cancel an
  email change, each exactly once.
- ``bootstrap_admin`` invites the first group administrator while none exists (F76).
- Anyone signed in ends their own other sessions (M8.3, X5).
- The worker applies confirmed email changes after the cooling-off
  (``apps.accounts.housekeeping``).
"""

from apps.accounts.services.common import (
    EMAIL_CHANGE_CANCELLABLE,
    EMAIL_CHANGE_CONFIRM_MAIL,
    EMAIL_CHANGE_CONFIRMABLE,
    EMAIL_CHANGE_NOTICE_MAIL,
    INVITATION_MAIL,
    INVITATION_PENDING,
    MFA_RESET_MAIL,
    REASON_OFFBOARDED,
    REASON_REPLACED,
    REASON_RESENT,
)
from apps.accounts.services.credentials import (
    MFA_SECRET_FIELDS,
    MfaReencryption,
    end_other_sessions,
    end_own_session,
    reencrypt_mfa_secrets,
    reset_user_mfa,
)
from apps.accounts.services.email_changes import (
    cancel_email_change,
    confirm_email_change,
    request_email_change,
    revoke_email_change,
)
from apps.accounts.services.grants import AssignmentGrant, MembershipGrant, parse_grants
from apps.accounts.services.invitations import (
    Bootstrap,
    accept_invitation,
    bootstrap_admin,
    create_invitation,
    resend_invitation,
    revoke_invitation,
)
from apps.accounts.services.offboarding import offboard_user

__all__ = [
    "EMAIL_CHANGE_CANCELLABLE",
    "EMAIL_CHANGE_CONFIRMABLE",
    "EMAIL_CHANGE_CONFIRM_MAIL",
    "EMAIL_CHANGE_NOTICE_MAIL",
    "INVITATION_MAIL",
    "INVITATION_PENDING",
    "MFA_RESET_MAIL",
    "MFA_SECRET_FIELDS",
    "REASON_OFFBOARDED",
    "REASON_REPLACED",
    "REASON_RESENT",
    "AssignmentGrant",
    "Bootstrap",
    "MembershipGrant",
    "MfaReencryption",
    "accept_invitation",
    "bootstrap_admin",
    "cancel_email_change",
    "confirm_email_change",
    "create_invitation",
    "end_other_sessions",
    "end_own_session",
    "offboard_user",
    "parse_grants",
    "reencrypt_mfa_secrets",
    "request_email_change",
    "resend_invitation",
    "reset_user_mfa",
    "revoke_email_change",
    "revoke_invitation",
]
