"""Revoking the pending tokens of an account (plan §5, "Offboarding" and F71).

Called inside a transaction that is already authorized and holds the affected ``User`` rows:

- offboarding revokes every pending invitation and email change *for* the account (by user
  or by email) and every one it *initiated* for others;
- losing ``org.manage`` (a revoked membership, a validity window moved away from now)
  revokes the ones the administrator initiated.

Their unsent emails are expired in the same transaction. A token that loses its initiator's
authority by expiry alone is refused when used (``authorize_in_txn`` re-checks the initiator).
"""

from dataclasses import dataclass
from uuid import UUID

from django.db.models import Q

from apps.accounts.models import EmailChangeRequest, Invitation
from apps.core.db import db_clock
from apps.notifications.outbox import expire_security_email


@dataclass(frozen=True)
class Revoked:
    invitations: list[Invitation]
    email_changes: list[EmailChangeRequest]


def revoke_pending_tokens(
    *,
    reason: str,
    initiated_by: UUID,
    user_id: UUID | None = None,
    email: str | None = None,
) -> Revoked:
    """Revoke the pending tokens ``initiated_by`` issued, and those for ``user_id`` / ``email``."""
    invitation_q = Q(invited_by_id=initiated_by)
    change_q = Q(requested_by_id=initiated_by)
    if user_id is not None:
        invitation_q |= Q(target_user_id=user_id)
        change_q |= Q(user_id=user_id)
    if email:
        invitation_q |= Q(email=email.lower())
    now = db_clock()
    invitations = list(
        Invitation.objects.select_for_update()
        .filter(invitation_q, accepted_at__isnull=True, revoked_at__isnull=True)
        .order_by("pk")
    )
    changes = list(
        EmailChangeRequest.objects.select_for_update()
        .filter(
            change_q, applied_at__isnull=True, cancelled_at__isnull=True, revoked_at__isnull=True
        )
        .order_by("pk")
    )
    for invitation in invitations:
        invitation.revoked_at = now
        invitation.revoke_reason = reason
        invitation.save(update_fields=["revoked_at", "revoke_reason", "updated_at"])
    for change in changes:
        change.revoked_at = now
        change.revoke_reason = reason
        change.save(update_fields=["revoked_at", "revoke_reason", "updated_at"])
    expire_security_email(targets=[*invitations, *changes])
    return Revoked(invitations, changes)
