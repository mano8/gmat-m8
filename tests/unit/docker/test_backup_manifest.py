"""The backup manifest query (``docker/backup/manifest.sql``) on the real schema (plan §9).

``backup.sh`` runs it through psql inside the dump's snapshot and ``restore.sh`` on the
restored copy. Here it runs on the test database, with psql's ``:'audit_table'`` replaced
by the literal psql would put there.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from django.db import connection, transaction
from django.utils import timezone

from apps.attachments.files import new_storage_key
from apps.attachments.models import Attachment
from apps.audit.models import AuditEvent
from apps.audit.recorder import AuditIdentity, record
from apps.material_requests.models import MaterialRequest
from tests.org import Org, People

ROOT = Path(__file__).resolve().parents[3]
MANIFEST_SQL = ROOT / "docker" / "backup" / "manifest.sql"
AUDIT_TABLE = AuditEvent._meta.db_table

pytestmark = pytest.mark.django_db


def _manifest(audit_table: str = AUDIT_TABLE) -> dict[str, Any]:
    sql = MANIFEST_SQL.read_text(encoding="utf-8")
    assert ":'audit_table'" in sql
    with connection.cursor() as cursor:
        cursor.execute(sql.replace(":'audit_table'", f"'{audit_table}'"))
        row = cursor.fetchone()
    assert row is not None
    value = row[0]
    return value if isinstance(value, dict) else json.loads(value)


def test_an_empty_audit_table_gives_no_newest_event() -> None:
    # A fresh install: the audit table exists from its migration but holds no event yet.
    assert not AuditEvent.objects.exists()
    manifest = _manifest()
    assert manifest["audit_last_event_id"] is None
    assert manifest["row_counts"][AUDIT_TABLE] == 0
    assert "audit" in manifest["migration_head"]


def test_the_newest_audit_event_is_the_highest_uuidv7() -> None:
    identity = AuditIdentity(label="system:worker", via="system:worker")
    with transaction.atomic():
        events = [record(identity, "manifest.checked") for _ in range(3)]
    manifest = _manifest()
    assert manifest["audit_last_event_id"] == str(max(event.pk for event in events))
    assert manifest["row_counts"][AUDIT_TABLE] == 3


def test_no_audit_table_gives_no_newest_event() -> None:
    assert _manifest("audit_table_not_created_yet")["audit_last_event_id"] is None


def test_no_attachment_gives_an_empty_list() -> None:
    assert _manifest()["attachments"] == []


def test_the_stored_attachments_are_listed_for_the_media_copy(org: Org, people: People) -> None:
    """backup.sh copies exactly these files and checks each against its digest (M6.4)."""
    draft = MaterialRequest.objects.create(
        project=org.hospital, procurement_branch=org.a_mad, requested_by=people.tech_mad
    )
    keys = sorted(new_storage_key() for _ in range(2))
    for index, key in enumerate(keys):
        Attachment.objects.create(
            request=draft,
            storage_key=key,
            file_name=f"{index}.pdf",
            content_type=Attachment.ContentType.PDF,
            size_bytes=10 + index,
            sha256=f"{index}" * 64,
            uploaded_by=people.tech_mad,
            uploaded_at=timezone.now(),
        )
    assert _manifest()["attachments"] == [
        {"storage_key": key, "size_bytes": 10 + index, "sha256": f"{index}" * 64}
        for index, key in enumerate(keys)
    ]
