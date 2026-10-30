"""Generation counters follow every scope-defining change (plan §3.6).

- ``AccessState.org_generation`` is bumped when a company is created or its ``is_active``
  changes, when a branch is created or its ``is_active``, ``managed_by`` or ``company``
  changes, and when a project is created or its ``branch``, ``procurement_branch`` or
  ``status`` changes. The bump is an upsert, so it also recreates the singleton row.
- ``User.authz_generation`` is bumped for the user of every membership or project
  assignment that is created or changed (validity, revocation). Deletes are refused by
  the tables' own triggers.

Services lock ``AccessState`` and the target ``User`` row first (L0), so the row locks
these triggers take are already held. Reversible: the reverse drops every trigger and
function.
"""

from django.db import migrations

# The singleton's id (``apps.access.models.ACCESS_STATE_ID``) is written into the SQL as a
# literal: the text is fixed, nothing is interpolated.
FORWARD = """
CREATE FUNCTION access_bump_org_generation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO access_accessstate (id, org_generation) VALUES ('00000000-0000-0000-0000-000000000001', 1)
    ON CONFLICT (id) DO UPDATE SET org_generation = access_accessstate.org_generation + 1;
    RETURN NULL;
END;
$$;

CREATE TRIGGER organizations_company_org_generation_insert
AFTER INSERT ON organizations_company
FOR EACH ROW EXECUTE FUNCTION access_bump_org_generation();

CREATE TRIGGER organizations_company_org_generation_update
AFTER UPDATE ON organizations_company
FOR EACH ROW WHEN (OLD.is_active IS DISTINCT FROM NEW.is_active)
EXECUTE FUNCTION access_bump_org_generation();

CREATE TRIGGER organizations_branch_org_generation_insert
AFTER INSERT ON organizations_branch
FOR EACH ROW EXECUTE FUNCTION access_bump_org_generation();

CREATE TRIGGER organizations_branch_org_generation_update
AFTER UPDATE ON organizations_branch
FOR EACH ROW WHEN (
    OLD.is_active IS DISTINCT FROM NEW.is_active
    OR OLD.managed_by_id IS DISTINCT FROM NEW.managed_by_id
    OR OLD.company_id IS DISTINCT FROM NEW.company_id
)
EXECUTE FUNCTION access_bump_org_generation();

CREATE TRIGGER projects_project_org_generation_insert
AFTER INSERT ON projects_project
FOR EACH ROW EXECUTE FUNCTION access_bump_org_generation();

CREATE TRIGGER projects_project_org_generation_update
AFTER UPDATE ON projects_project
FOR EACH ROW WHEN (
    OLD.branch_id IS DISTINCT FROM NEW.branch_id
    OR OLD.procurement_branch_id IS DISTINCT FROM NEW.procurement_branch_id
    OR OLD.status IS DISTINCT FROM NEW.status
)
EXECUTE FUNCTION access_bump_org_generation();

CREATE FUNCTION access_bump_authz_generation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    UPDATE accounts_user SET authz_generation = authz_generation + 1 WHERE id = NEW.user_id;
    IF TG_OP = 'UPDATE' AND OLD.user_id IS DISTINCT FROM NEW.user_id THEN
        UPDATE accounts_user SET authz_generation = authz_generation + 1
        WHERE id = OLD.user_id;
    END IF;
    RETURN NULL;
END;
$$;

CREATE TRIGGER organizations_membership_authz_generation
AFTER INSERT OR UPDATE ON organizations_membership
FOR EACH ROW EXECUTE FUNCTION access_bump_authz_generation();

CREATE TRIGGER projects_assignment_authz_generation
AFTER INSERT OR UPDATE ON projects_projectassignment
FOR EACH ROW EXECUTE FUNCTION access_bump_authz_generation();
"""

REVERSE = """
DROP TRIGGER IF EXISTS projects_assignment_authz_generation ON projects_projectassignment;
DROP TRIGGER IF EXISTS organizations_membership_authz_generation ON organizations_membership;
DROP FUNCTION IF EXISTS access_bump_authz_generation();
DROP TRIGGER IF EXISTS projects_project_org_generation_update ON projects_project;
DROP TRIGGER IF EXISTS projects_project_org_generation_insert ON projects_project;
DROP TRIGGER IF EXISTS organizations_branch_org_generation_update ON organizations_branch;
DROP TRIGGER IF EXISTS organizations_branch_org_generation_insert ON organizations_branch;
DROP TRIGGER IF EXISTS organizations_company_org_generation_update ON organizations_company;
DROP TRIGGER IF EXISTS organizations_company_org_generation_insert ON organizations_company;
DROP FUNCTION IF EXISTS access_bump_org_generation();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("access", "0001_initial"),
        ("accounts", "0002_user_security_counters"),
        ("organizations", "0001_initial"),
        ("projects", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(FORWARD, REVERSE),
    ]
