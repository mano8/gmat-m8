"""The storage quota and the reservations, under their L8 locks (plan §4.7, §5).

Helpers for authorized callers (``services``, and the worker's ``housekeeping``, authorized
by ``run_housekeeping``); they never authorize on their own. Every function expects the
caller's transaction and its ``LockOrder``: the parent headers first, then the quota row,
then the reservation rows, all at L8.
"""

import logging
from datetime import datetime
from uuid import UUID

from django.conf import settings
from django.db import connections
from django.db.models import Model

from apps.attachments import files
from apps.attachments.models import QUOTA_ID, AttachmentReservation, StorageQuota
from apps.attachments.parents import ParentType
from apps.core.locking import LockLevel, LockOrder, LockSetChanged

logger = logging.getLogger(__name__)

State = AttachmentReservation.State


def lock_parent(locks: LockOrder, ptype: ParentType, parent: Model) -> Model:
    """Lock the parent header at its level and re-read it.

    What the authorization read must still hold (``ParentType.verified``), or the retry
    wrapper runs the service again and authorizes again.
    """
    locks.lock(ptype.level, ptype.model, [parent.pk])
    locked = ptype.model._default_manager.get(pk=parent.pk)
    for name in ptype.verified:
        if getattr(locked, name) != getattr(parent, name):
            raise LockSetChanged("The document changed between authorization and locking")
    return locked


def lock_quota(locks: LockOrder) -> StorageQuota:
    """Lock the quota row (L8), created with or brought to ``ATTACHMENT_STORAGE_LIMIT``.

    The row comes from ``attachments.0002``; it is recreated if a database flush removed it.
    The setting is the authority on the limit, so a change applies at the next upload.
    """
    locks.enter(LockLevel.STORAGE)
    table = StorageQuota._meta.db_table
    with connections[locks.using].cursor() as cursor:
        cursor.execute(
            f"INSERT INTO {table} (id, limit_bytes, used_bytes, reserved_bytes)"  # noqa: S608  # nosec B608
            " VALUES (%s, %s, 0, 0) ON CONFLICT (id) DO UPDATE"
            " SET limit_bytes = EXCLUDED.limit_bytes"
            f" WHERE {table}.limit_bytes <> EXCLUDED.limit_bytes",
            [QUOTA_ID, settings.ATTACHMENT_STORAGE_LIMIT],
        )
    locks.lock(LockLevel.STORAGE, StorageQuota, [QUOTA_ID])
    return StorageQuota.objects.get(pk=QUOTA_ID)


def lock_reservations(locks: LockOrder, ids: list[UUID]) -> list[AttachmentReservation]:
    """Lock reservation rows after the quota (L8) and re-read them."""
    locks.lock(LockLevel.STORAGE, AttachmentReservation, ids)
    return list(AttachmentReservation.objects.filter(pk__in=ids).order_by("pk"))


def headroom(quota: StorageQuota) -> int:
    return quota.limit_bytes - quota.used_bytes - quota.reserved_bytes


def release(quota: StorageQuota, reservation: AttachmentReservation, now: datetime) -> None:
    """Give a ``PROCESSING`` reservation's bytes back and mark it ``RELEASED``.

    Its staged files go with it; a late finalization then finds ``RELEASED`` and fails.
    """
    quota.reserved_bytes -= reservation.reserved_bytes
    quota.save(update_fields=["reserved_bytes"])
    reservation.state, reservation.finished_at = State.RELEASED, now
    reservation.save(update_fields=["state", "finished_at", "updated_at"])
    files.discard_staged(reservation.pk)


def check_alert(quota: StorageQuota, now: datetime) -> bool:
    """Raise the 80 % alert once when the quota crosses it, clear it below (Q22).

    Returns ``True`` when the alert was just raised (the caller audits it).
    """
    above = (quota.used_bytes + quota.reserved_bytes) * 100 >= (
        quota.limit_bytes * settings.ATTACHMENT_ALERT_PERCENT
    )
    if above == (quota.alert_raised_at is not None):
        return False
    quota.alert_raised_at = now if above else None
    quota.save(update_fields=["alert_raised_at"])
    if above:
        logger.warning(
            "Attachment storage above %s %% of its limit (%s of %s bytes)",
            settings.ATTACHMENT_ALERT_PERCENT,
            quota.used_bytes + quota.reserved_bytes,
            quota.limit_bytes,
        )
    return above
