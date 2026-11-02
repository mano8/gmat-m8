"""Composite foreign keys of handover lines (plan §4.6) and their immutability (I11).

- C3: ``(request_line_id, project_id) → materialrequestline(id, project_id)`` and
  ``(handover_id, project_id) → handover(id, project_id)``: a handover line's request line
  belongs to the handover's project.
- I11: a row trigger lets a posted handover change only by its void (status, the void
  fields and ``updated_at``; the CHECK ``ho_void_fields`` keeps them together) and never
  again once voided; handovers are never deleted, and their lines are never updated or
  deleted. Refusals raise ``restrict_violation`` (an ``IntegrityError``).

Reversible: the reverse drops the triggers, their functions and the constraints.
"""

from django.db import migrations

from apps.core.migration_ops import AddCompositeForeignKey

FORWARD = """
CREATE FUNCTION handovers_handover_posted() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'handovers are never deleted'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF OLD.status <> 'POSTED'
        OR (to_jsonb(NEW) - ARRAY['status', 'voided_at', 'voided_by_id', 'void_reason', 'updated_at'])
            IS DISTINCT FROM
           (to_jsonb(OLD) - ARRAY['status', 'voided_at', 'voided_by_id', 'void_reason', 'updated_at'])
    THEN
        RAISE EXCEPTION 'a posted handover changes only by its void'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER handovers_handover_posted
BEFORE UPDATE OR DELETE ON handovers_handover
FOR EACH ROW EXECUTE FUNCTION handovers_handover_posted();

CREATE FUNCTION handovers_line_frozen() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'handover lines are never changed: % refused', TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$;

CREATE TRIGGER handovers_line_frozen
BEFORE UPDATE OR DELETE ON handovers_handoverline
FOR EACH ROW EXECUTE FUNCTION handovers_line_frozen();
"""

REVERSE = """
DROP TRIGGER IF EXISTS handovers_line_frozen ON handovers_handoverline;
DROP FUNCTION IF EXISTS handovers_line_frozen();
DROP TRIGGER IF EXISTS handovers_handover_posted ON handovers_handover;
DROP FUNCTION IF EXISTS handovers_handover_posted();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("handovers", "0001_initial"),
    ]

    operations = [
        AddCompositeForeignKey(
            name="ho_line_request_line_project_fk",
            table="handovers_handoverline",
            columns=("request_line_id", "project_id"),
            ref_table="material_requests_materialrequestline",
            ref_columns=("id", "project_id"),
        ),
        AddCompositeForeignKey(
            name="ho_line_handover_project_fk",
            table="handovers_handoverline",
            columns=("handover_id", "project_id"),
            ref_table="handovers_handover",
            ref_columns=("id", "project_id"),
        ),
        migrations.RunSQL(FORWARD, REVERSE),
    ]
