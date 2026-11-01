"""Attachments are immutable except for their removal; the quota row (plan §5).

- A row trigger lets an attachment change only by its removal (``removed_at``,
  ``removed_by_id``, ``removal_reason`` and ``updated_at``), once: the parent link, the
  stored file and its digest never change, so an attachment can never be relinked to
  another document. A row is deleted only once removed (the retention purge). Refusals
  raise ``restrict_violation`` (an ``IntegrityError``).
- The ``StorageQuota`` singleton, with the limit of ``ATTACHMENT_STORAGE_LIMIT`` (the setting
  stays the authority: ``quota.lock_quota`` applies a change at the next upload).

Reversible: the reverse drops the trigger and its function and deletes the quota row.
"""

from typing import Any

from django.conf import settings
from django.db import migrations

QUOTA_ID = "00000000-0000-0000-0000-000000000002"

FORWARD = """
CREATE FUNCTION attachments_attachment_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.removed_at IS NULL THEN
            RAISE EXCEPTION 'an attachment is deleted only by the purge, once removed'
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN OLD;
    END IF;
    IF OLD.removed_at IS NOT NULL
        OR (to_jsonb(NEW) - ARRAY['removed_at', 'removed_by_id', 'removal_reason', 'updated_at'])
            IS DISTINCT FROM
           (to_jsonb(OLD) - ARRAY['removed_at', 'removed_by_id', 'removal_reason', 'updated_at'])
    THEN
        RAISE EXCEPTION 'an attachment changes only by its removal, once'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER attachments_attachment_guard
BEFORE UPDATE OR DELETE ON attachments_attachment
FOR EACH ROW EXECUTE FUNCTION attachments_attachment_guard();
"""

REVERSE = """
DROP TRIGGER IF EXISTS attachments_attachment_guard ON attachments_attachment;
DROP FUNCTION IF EXISTS attachments_attachment_guard();
"""


def create_quota(apps: Any, schema_editor: Any) -> None:
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO attachments_storagequota (id, limit_bytes, used_bytes, reserved_bytes)"
            " VALUES (%s, %s, 0, 0) ON CONFLICT (id) DO NOTHING",
            [QUOTA_ID, settings.ATTACHMENT_STORAGE_LIMIT],
        )


def delete_quota(apps: Any, schema_editor: Any) -> None:
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("DELETE FROM attachments_storagequota WHERE id = %s", [QUOTA_ID])


class Migration(migrations.Migration):
    dependencies = [
        ("attachments", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(FORWARD, REVERSE),
        migrations.RunPython(create_quota, delete_quota),
    ]
