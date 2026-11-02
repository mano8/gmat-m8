"""The worker's attachment tasks (plan §5 "Expiry", §9 "Media retention coordination").

Registered from ``AppConfig.ready``; ``apps.notifications.services.run_housekeeping`` runs
each in its own transaction, authorized as ``housekeeping.<name>`` for ``system:worker``.

- ``attachment_reservations``: releases every reservation whose lease has expired, under
  the same locks as finalization (parents, then the quota and the reservations at L8).
  Every limit of an upload ends well inside the lease, so an expired lease means its work
  has stopped, and a late finalization would find ``RELEASED`` anyway. Finished
  reservations older than a week and staged files nobody holds are deleted too.
- ``attachment_media``: the two-phase purge, only while it holds the ``gmat:media-backup``
  advisory lock (a backup holding it postpones the purge to the next run). Phase 1 deletes
  the rows of attachments removed more than ``ATTACHMENT_RETENTION_DAYS`` ago and queues
  their files; phase 2 deletes queued files older than ``MEDIA_PURGE_GRACE`` (longer than
  any backup copy) and gives their bytes back to the quota.
"""

import time
from collections.abc import Iterable
from datetime import timedelta

from django.conf import settings
from django.db.models import Model

from apps.attachments import files
from apps.attachments.models import Attachment, AttachmentReservation, MediaDeletion
from apps.attachments.parents import PARENTS
from apps.attachments.quota import check_alert, lock_quota, lock_reservations, release
from apps.audit.recorder import AuditIdentity, record
from apps.core.db import db_now
from apps.core.locking import MEDIA_BACKUP_LOCK, LockOrder, try_advisory_xact_lock

State = AttachmentReservation.State
WORKER = AuditIdentity(label="system:worker", via="system:worker")
FINISHED_RETENTION = timedelta(days=7)


def _lock_parents(locks: LockOrder, rows: Iterable[Model]) -> None:
    """Lock the parent headers of these rows, level by level (§4.7)."""
    rows = list(rows)
    for ptype in sorted(PARENTS.values(), key=lambda candidate: candidate.level):
        ids = {getattr(row, f"{ptype.field}_id") for row in rows} - {None}
        locks.lock(ptype.level, ptype.model, ids)


def _sweep_staging(live: set[str]) -> int:
    """Delete staged files of no live reservation, once older than two leases."""
    folder = files.staging_dir()
    if not folder.is_dir():
        return 0
    cutoff = time.time() - 2 * settings.ATTACHMENT_LEASE
    swept = 0
    for path in folder.iterdir():
        name = path.name.split(".", 1)[0]
        if name not in live and path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)
            swept += 1
    return swept


def release_expired_reservations() -> int:
    now = db_now()
    expired = list(
        AttachmentReservation.objects.filter(state=State.PROCESSING, lease_until__lte=now)
    )
    released = 0
    if expired:
        locks = LockOrder()
        _lock_parents(locks, expired)
        quota = lock_quota(locks)
        for row in lock_reservations(locks, [row.pk for row in expired]):
            if row.state == State.PROCESSING and row.lease_until <= now:
                release(quota, row, now)
                released += 1
        check_alert(quota, now)
    old = AttachmentReservation.objects.exclude(state=State.PROCESSING).filter(
        finished_at__lt=now - FINISHED_RETENTION
    )
    deleted, _ = old.delete()
    live = AttachmentReservation.objects.filter(state=State.PROCESSING).values_list("pk", flat=True)
    return released + deleted + _sweep_staging({str(pk) for pk in live})


def _purge_rows(locks: LockOrder) -> int:
    """Phase 1: the rows of attachments removed before the retention, files queued."""
    cutoff = db_now() - timedelta(days=settings.ATTACHMENT_RETENTION_DAYS)
    due = list(Attachment.objects.filter(removed_at__lte=cutoff).order_by("pk"))
    _lock_parents(locks, due)
    delete_after = db_now() + timedelta(seconds=settings.MEDIA_PURGE_GRACE)
    for attachment in due:
        MediaDeletion.objects.create(
            storage_key=attachment.storage_key,
            size_bytes=attachment.size_bytes,
            delete_after=delete_after,
        )
        record(
            WORKER,
            "attachment.purged",
            obj=attachment,
            data={"storage_key": attachment.storage_key, "size_bytes": attachment.size_bytes},
        )
        attachment.delete()
    return len(due)


def _delete_files(locks: LockOrder) -> int:
    """Phase 2: files queued longer than the grace period; their bytes go back."""
    now = db_now()
    due = list(MediaDeletion.objects.filter(delete_after__lte=now).order_by("pk"))
    if not due:
        return 0
    quota = lock_quota(locks)
    for item in due:
        files.discard(files.stored_path(item.storage_key))
        quota.used_bytes = max(0, quota.used_bytes - item.size_bytes)
        item.delete()
    quota.save(update_fields=["used_bytes"])
    check_alert(quota, now)
    return len(due)


def purge_media() -> int:
    if not try_advisory_xact_lock(MEDIA_BACKUP_LOCK):
        return 0  # a backup is copying the media: next run
    locks = LockOrder()
    return _purge_rows(locks) + _delete_files(locks)
