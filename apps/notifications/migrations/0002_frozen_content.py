"""Freeze outbox content at insert; keep ``current_attempt`` on its own message (plan §4.2).

- A row trigger refuses any ``UPDATE`` that changes what a message says or to whom: kind,
  recipients, eligibility, source, sender, subject, expiry. A body may only be wiped (set to
  the empty string), ``wiped_at`` and ``message_id`` only set once. Status, attempts, timing
  and errors stay writable for the worker.
- A composite foreign key ``(current_attempt_id, id) → emailattempt (id, message_id)``: a
  message can only point at one of its own attempts.

Reversible: the reverse drops the trigger, its function and the constraint.
"""

from django.db import migrations

from apps.core.migration_ops import AddCompositeForeignKey

FORWARD = """
CREATE FUNCTION notifications_outbox_frozen() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.kind IS DISTINCT FROM OLD.kind
        OR NEW.sensitive IS DISTINCT FROM OLD.sensitive
        OR NEW.recipient_users IS DISTINCT FROM OLD.recipient_users
        OR NEW.recipient_address IS DISTINCT FROM OLD.recipient_address
        OR NEW.eligibility_rule IS DISTINCT FROM OLD.eligibility_rule
        OR NEW.eligibility_object_type IS DISTINCT FROM OLD.eligibility_object_type
        OR NEW.eligibility_object_id IS DISTINCT FROM OLD.eligibility_object_id
        OR NEW.source_po IS DISTINCT FROM OLD.source_po
        OR NEW.source_ref IS DISTINCT FROM OLD.source_ref
        OR NEW.from_email IS DISTINCT FROM OLD.from_email
        OR NEW.subject IS DISTINCT FROM OLD.subject
        OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
        OR (NEW.body IS DISTINCT FROM OLD.body AND NEW.body <> '')
        OR (NEW.html_body IS DISTINCT FROM OLD.html_body AND NEW.html_body <> '')
        OR (OLD.wiped_at IS NOT NULL AND NEW.wiped_at IS DISTINCT FROM OLD.wiped_at)
        OR (OLD.message_id <> '' AND NEW.message_id IS DISTINCT FROM OLD.message_id)
    THEN
        RAISE EXCEPTION 'outbox content is frozen at insert'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER notifications_outbox_frozen
BEFORE UPDATE ON notifications_emailoutbox
FOR EACH ROW EXECUTE FUNCTION notifications_outbox_frozen();
"""

REVERSE = """
DROP TRIGGER IF EXISTS notifications_outbox_frozen ON notifications_emailoutbox;
DROP FUNCTION IF EXISTS notifications_outbox_frozen();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("notifications", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(FORWARD, REVERSE),
        AddCompositeForeignKey(
            name="notifications_outbox_current_attempt_own",
            table="notifications_emailoutbox",
            columns=("current_attempt_id", "id"),
            ref_table="notifications_emailattempt",
            ref_columns=("id", "message_id"),
        ),
    ]
