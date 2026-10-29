"""Database guard for the user's security counters (plan §3.6).

- ``session_epoch`` and ``authz_generation`` never decrease, so saving a stale copy of a
  user cannot revive ended sessions or revoked authority.
- A password change, an email change or a deactivation bumps ``session_epoch``: every
  existing session of that user ends, whichever code path made the change.
- A change to ``is_active``, ``is_staff`` or ``is_superuser`` bumps ``authz_generation``.

Reversible: the reverse drops the trigger and its function.
"""

from django.db import migrations

FORWARD = """
CREATE FUNCTION accounts_user_security_counters() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.session_epoch := GREATEST(NEW.session_epoch, OLD.session_epoch);
    NEW.authz_generation := GREATEST(NEW.authz_generation, OLD.authz_generation);
    IF NEW.password IS DISTINCT FROM OLD.password
       OR NEW.email IS DISTINCT FROM OLD.email
       OR (OLD.is_active AND NOT NEW.is_active) THEN
        NEW.session_epoch := GREATEST(NEW.session_epoch, OLD.session_epoch + 1);
    END IF;
    IF NEW.is_active IS DISTINCT FROM OLD.is_active
       OR NEW.is_staff IS DISTINCT FROM OLD.is_staff
       OR NEW.is_superuser IS DISTINCT FROM OLD.is_superuser THEN
        NEW.authz_generation := GREATEST(NEW.authz_generation, OLD.authz_generation + 1);
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER accounts_user_security_counters
BEFORE UPDATE ON accounts_user
FOR EACH ROW EXECUTE FUNCTION accounts_user_security_counters();
"""

REVERSE = """
DROP TRIGGER IF EXISTS accounts_user_security_counters ON accounts_user;
DROP FUNCTION IF EXISTS accounts_user_security_counters();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(FORWARD, REVERSE),
    ]
