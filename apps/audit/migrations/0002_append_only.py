"""Make ``audit_auditevent`` append-only (plan §4.2, CLAUDE.md "Identifiers").

A row trigger refuses every ``UPDATE`` and ``DELETE`` with ``restrict_violation`` (an
``IntegrityError`` in Django), whatever code path or database client tries it. ``TRUNCATE``
is kept from the runtime role by its privileges (``docker/postgres/initdb/10-roles.sh``).

Reversible: the reverse drops the trigger and its function.
"""

from django.db import migrations

FORWARD = """
CREATE FUNCTION audit_auditevent_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit events are append-only: % refused', TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$;

CREATE TRIGGER audit_auditevent_append_only
BEFORE UPDATE OR DELETE ON audit_auditevent
FOR EACH ROW EXECUTE FUNCTION audit_auditevent_append_only();
"""

REVERSE = """
DROP TRIGGER IF EXISTS audit_auditevent_append_only ON audit_auditevent;
DROP FUNCTION IF EXISTS audit_auditevent_append_only();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("audit", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(FORWARD, REVERSE),
    ]
