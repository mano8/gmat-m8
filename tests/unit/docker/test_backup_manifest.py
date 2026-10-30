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

from apps.audit.models import AuditEvent
from apps.audit.recorder import AuditIdentity, record

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
