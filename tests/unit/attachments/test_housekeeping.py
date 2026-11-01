"""The worker's attachment tasks (plan §5 "Expiry", §9 "Media retention coordination"):
expired leases are released, and the media purge runs in two phases and only while no
backup holds the ``gmat:media-backup`` lock.
"""

import os
import time
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.db import connections
from django.utils import timezone

from apps.access.principals import SessionPrincipal
from apps.attachments import files, housekeeping, services
from apps.attachments.models import (
    QUOTA_ID,
    Attachment,
    AttachmentReservation,
    MediaDeletion,
    StorageQuota,
)
from apps.audit.models import AuditEvent
from apps.core.locking import MEDIA_BACKUP_LOCK
from apps.material_requests.models import MaterialRequest
from apps.notifications import housekeeping as registry
from tests.unit.attachments.conftest import Attach, admit, stage

pytestmark = pytest.mark.django_db

State = AttachmentReservation.State


def quota() -> StorageQuota:
    return StorageQuota.objects.get(pk=QUOTA_ID)


def test_both_tasks_run_with_the_worker() -> None:
    tasks = registry.tasks()
    assert tasks["attachment_reservations"] is housekeeping.release_expired_reservations
    assert tasks["attachment_media"] is housekeeping.purge_media


class TestReservations:
    def test_an_expired_lease_is_released(
        self, draft: MaterialRequest, tech: SessionPrincipal
    ) -> None:
        expired, live = admit(tech, draft, 3000), admit(tech, draft, 4000)
        stage(expired, b"half a file")
        AttachmentReservation.objects.filter(pk=expired.pk).update(
            lease_until=timezone.now() - timedelta(seconds=1)
        )
        assert housekeeping.release_expired_reservations() == 1
        expired.refresh_from_db()
        live.refresh_from_db()
        assert (expired.state, live.state) == (State.RELEASED, State.PROCESSING)
        assert quota().reserved_bytes == 4000
        assert not files.staged_input(expired.pk).exists()
        # A late finalization of the expired upload now fails.
        stage(expired, b"half a file")
        with pytest.raises(ValidationError):
            services.finalize_upload(tech, expired.pk, file_name="late.png")

    def test_old_finished_reservations_are_deleted(
        self, draft: MaterialRequest, tech: SessionPrincipal
    ) -> None:
        old, recent = admit(tech, draft, 100), admit(tech, draft, 100)
        for item in (old, recent):
            services.release_reservation(tech, item.pk)
        AttachmentReservation.objects.filter(pk=old.pk).update(
            finished_at=timezone.now() - timedelta(days=8)
        )
        assert housekeeping.release_expired_reservations() == 1
        assert list(AttachmentReservation.objects.values_list("pk", flat=True)) == [recent.pk]

    def test_staged_files_nobody_holds_are_swept(
        self, draft: MaterialRequest, tech: SessionPrincipal
    ) -> None:
        live = admit(tech, draft, 100)
        stage(live, b"in progress")
        files.ensure_dirs()
        orphan = files.staging_dir() / "0190f000-0000-7000-8000-000000000000.upload"
        fresh = files.staging_dir() / "0190f000-0000-7000-8000-000000000001.out"
        for path in (orphan, fresh):
            path.write_bytes(b"left behind")
        old = time.time() - 3 * 15 * 60
        os.utime(orphan, (old, old))
        os.utime(files.staged_input(live.pk), (old, old))
        assert housekeeping.release_expired_reservations() == 1
        assert not orphan.exists()
        assert fresh.exists()  # younger than two leases: maybe still in use
        assert files.staged_input(live.pk).exists()

    def test_nothing_to_do(self, settings: Any) -> None:
        assert housekeeping.release_expired_reservations() == 0


@pytest.fixture
def removed(
    settings: Any, draft: MaterialRequest, tech: SessionPrincipal, attach: Attach
) -> Attachment:
    """A file of the draft removed by its uploader, past a retention shortened to none."""
    item = attach(tech, draft)
    services.remove_attachment(tech, item.pk)
    settings.ATTACHMENT_RETENTION_DAYS = 0
    item.refresh_from_db()
    return item


@pytest.fixture
def backup_lock() -> Iterator[None]:
    """A backup session holding the media lock on its own connection, as backup.sh does."""
    other = connections.create_connection("default")
    try:
        with other.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", [MEDIA_BACKUP_LOCK])
        yield
        with other.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_unlock(hashtextextended(%s, 0))", [MEDIA_BACKUP_LOCK]
            )
    finally:
        other.close()


class TestPurge:
    def test_the_row_goes_first_and_the_file_after_the_grace_period(
        self, removed: Attachment, draft: MaterialRequest, tech: SessionPrincipal, attach: Attach
    ) -> None:
        kept = attach(tech, draft)  # not removed
        stored = files.stored_path(removed.storage_key)
        assert housekeeping.purge_media() == 1
        assert list(Attachment.objects.values_list("pk", flat=True)) == [kept.pk]
        assert stored.exists()  # a backup that listed it may still copy it
        deletion = MediaDeletion.objects.get()
        assert (deletion.storage_key, deletion.size_bytes) == (
            removed.storage_key,
            removed.size_bytes,
        )
        assert deletion.delete_after > timezone.now() + timedelta(hours=23)
        assert AuditEvent.objects.filter(action="attachment.purged").count() == 1
        used = quota().used_bytes
        assert housekeeping.purge_media() == 0  # within the grace period
        MediaDeletion.objects.update(delete_after=timezone.now() - timedelta(seconds=1))
        assert housekeeping.purge_media() == 1
        assert not stored.exists()
        assert not MediaDeletion.objects.exists()
        assert quota().used_bytes == used - removed.size_bytes
        assert files.stored_path(kept.storage_key).exists()

    def test_a_file_removed_within_the_retention_stays(
        self, draft: MaterialRequest, tech: SessionPrincipal, attach: Attach
    ) -> None:
        item = attach(tech, draft)
        services.remove_attachment(tech, item.pk)
        assert housekeeping.purge_media() == 0
        assert Attachment.objects.filter(pk=item.pk).exists()

    def test_a_purge_never_deletes_while_a_backup_holds_the_lock(
        self, removed: Attachment, backup_lock: None
    ) -> None:
        MediaDeletion.objects.create(
            storage_key=files.new_storage_key(), size_bytes=1, delete_after=timezone.now()
        )
        assert housekeeping.purge_media() == 0
        assert Attachment.objects.filter(pk=removed.pk).exists()
        assert MediaDeletion.objects.count() == 1
        assert files.stored_path(removed.storage_key).exists()

    def test_the_alert_clears_when_the_purge_frees_room(
        self, settings: Any, removed: Attachment
    ) -> None:
        StorageQuota.objects.update(alert_raised_at=timezone.now())
        housekeeping.purge_media()
        MediaDeletion.objects.update(delete_after=timezone.now() - timedelta(seconds=1))
        housekeeping.purge_media()
        assert quota().alert_raised_at is None
