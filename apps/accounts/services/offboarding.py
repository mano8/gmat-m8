"""Offboarding: an account, its grants and its pending tokens, in one transaction (F41, F71)."""

from uuid import UUID

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.access.principals import Principal
from apps.accounts.models import User
from apps.accounts.revocation import revoke_pending_tokens
from apps.accounts.services.common import REASON_OFFBOARDED, _lock_and_authorize
from apps.audit.recorder import record
from apps.core import events
from apps.core.db import db_clock
from apps.core.locking import transactional
from apps.notifications.outbox import expire_security_email
from apps.organizations.models import Membership
from apps.projects.models import ProjectAssignment


@transactional
def offboard_user(principal: Principal, user_id: UUID) -> User:
    """Offboard an account in one transaction (plan §5, "Offboarding", F41, F71).

    Deactivates it (``offboarded_at``; the triggers bump ``authz_generation`` and
    ``session_epoch``), revokes every membership and assignment, every pending invitation and
    email change for it or initiated by it, and expires its unsent security emails. An
    administrator cannot offboard their own account.
    """
    actor = _lock_and_authorize(principal, user_id)
    if actor.pk == user_id:
        raise ValidationError(_("You cannot offboard your own account."), code="self")
    user = User.objects.get(pk=user_id)
    if user.offboarded_at is not None:
        raise ValidationError(_("This account is already offboarded."), code="offboarded")
    now = db_clock()
    user.is_active = False
    user.offboarded_at = now
    user.save(update_fields=["is_active", "offboarded_at"])
    memberships = Membership.objects.filter(user=user, revoked_at__isnull=True).update(
        revoked_at=now, revoked_by=actor
    )
    assignments = ProjectAssignment.objects.filter(user=user, revoked_at__isnull=True).update(
        revoked_at=now, revoked_by=actor
    )
    revoked = revoke_pending_tokens(
        reason=REASON_OFFBOARDED, user_id=user.pk, email=user.email, initiated_by=user.pk
    )
    expired = expire_security_email(addresses=[user.email], targets=[user])
    record(
        principal.audit_identity,
        "user.offboarded",
        obj=user,
        data={
            "memberships_revoked": memberships,
            "assignments_revoked": assignments,
            "invitations_revoked": [item.pk for item in revoked.invitations],
            "email_changes_revoked": [item.pk for item in revoked.email_changes],
            "emails_expired": expired,
        },
    )
    events.publish("user.offboarded", user_id=user.pk)
    return user
