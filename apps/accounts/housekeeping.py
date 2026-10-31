"""The worker applies confirmed email changes after the cooling-off (plan §5, Q28).

Registered as the housekeeping task ``email_changes``: it runs in the worker's housekeeping
transaction, authorized as ``housekeeping.email_changes`` for ``system:worker``. Each due
request is applied only if, under the locks of plan §3.6 (``AccessState`` ``FOR SHARE``, the
account and initiator ``User`` rows, then the request), the account is still active, the
initiator still holds ``org.manage`` (a superuser for a staff account, F71) and the new
address is still free. Otherwise the request is revoked. Applying replaces the address,
marks it verified, and ends every session of the account (the ``accounts.0002`` trigger
bumps ``session_epoch`` on an email change).
"""

from django.db.models.functions import Now

from apps.access.context import AccessContext
from apps.access.exceptions import AccessDenied
from apps.access.generations import lock_access_state, lock_users
from apps.access.policy import require_may_manage_account
from apps.access.roles import Capability
from apps.accounts.models import EmailChangeRequest, User
from apps.audit.recorder import AuditIdentity, record
from apps.core import events
from apps.core.db import db_now
from apps.core.locking import LockLevel, LockMode, LockOrder

TASK_NAME = "email_changes"
BATCH = 100
WORKER = AuditIdentity(label="system:worker", via="system:worker")


def _refusal(change: EmailChangeRequest) -> str | None:
    user, initiator = change.user, change.requested_by
    if not user.is_active or user.offboarded_at is not None:
        return "account inactive"
    if not AccessContext.for_user(initiator).has(Capability.ORG_MANAGE):
        return "initiator lost org.manage"
    try:
        require_may_manage_account(initiator, user)
    except AccessDenied:
        return "initiator may not manage this account"
    if User.objects.filter(email=change.new_email).exclude(pk=user.pk).exists():
        return "address taken"
    return None


def apply_due_email_changes() -> int:
    """Apply (or revoke) the confirmed changes whose cooling-off is over; return how many."""
    due = list(
        EmailChangeRequest.objects.filter(
            confirmed_at__isnull=False,
            apply_after__lte=Now(),
            applied_at__isnull=True,
            cancelled_at__isnull=True,
            revoked_at__isnull=True,
        )
        .order_by("pk")
        .values_list("pk", "user_id", "requested_by_id")[:BATCH]
    )
    if not due:
        return 0
    locks = LockOrder()
    lock_access_state(locks)
    lock_users(locks, {uid for _, *ids in due for uid in ids}, mode=LockMode.NO_KEY_UPDATE)
    locks.lock(LockLevel.ACCESS, EmailChangeRequest, [pk for pk, *_ in due])
    done = 0
    now = db_now()
    for change in EmailChangeRequest.objects.select_related("user", "requested_by").filter(
        pk__in=[pk for pk, *_ in due],
        applied_at__isnull=True,
        cancelled_at__isnull=True,
        revoked_at__isnull=True,
    ):
        refusal = _refusal(change)
        if refusal is not None:
            change.revoked_at = now
            change.revoke_reason = refusal
            change.save(update_fields=["revoked_at", "revoke_reason", "updated_at"])
            record(WORKER, "email_change.revoked", obj=change, data={"reason": refusal})
            events.publish("email_change.revoked", change_id=change.pk)
            continue
        user = change.user
        old_email = user.email
        user.email = change.new_email
        user.save(update_fields=["email"])
        user.emailaddress_set.all().delete()
        user.emailaddress_set.create(email=user.email, primary=True, verified=True)
        change.applied_at = now
        change.save(update_fields=["applied_at", "updated_at"])
        record(
            WORKER,
            "email_change.applied",
            obj=change,
            data={"user_id": user.pk, "from": old_email, "to": user.email},
        )
        events.publish("email_change.applied", change_id=change.pk, user_id=user.pk)
        done += 1
    return done
