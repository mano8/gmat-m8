#!/bin/sh
# Creates the three application database roles (plan §5 "Data", §7.2).
#
#   owner    owns the database and its objects; used only by the one-shot `migrate` service
#   runtime  data access only (SELECT/INSERT/UPDATE/DELETE); used by `web` and `worker`;
#            it owns nothing, so it cannot alter or drop tables, triggers or the audit table
#   backup   pg_read_all_data and nothing else; used by pg_dump
#
# Runs once, from /docker-entrypoint-initdb.d, as the bootstrap superuser. It is idempotent
# (re-running it resets the passwords and grants) and refuses weak or placeholder values:
# a role with a template password must never exist, so the script fails the first start.
#
# Inputs (all required):
#   POSTGRES_DB, POSTGRES_USER, POSTGRES_PASSWORD            bootstrap database and superuser
#   GMAT_DB_OWNER_USER / GMAT_DB_OWNER_PASSWORD
#   GMAT_DB_RUNTIME_USER / GMAT_DB_RUNTIME_PASSWORD
#   GMAT_DB_BACKUP_USER / GMAT_DB_BACKUP_PASSWORD
set -eu

fail() {
    echo "10-roles.sh: $*" >&2
    exit 1
}

require() {
    eval "value=\${$1:-}"
    [ -n "$value" ] || fail "$1 is required"
}

check_name() {
    eval "value=\${$1}"
    case "$value" in
        *[!a-z0-9_]* | [!a-z]*) fail "$1 must match ^[a-z][a-z0-9_]*\$ (got an invalid name)" ;;
    esac
    [ "${#value}" -ge 3 ] && [ "${#value}" -le 30 ] || fail "$1 must be 3 to 30 characters"
    case "$value" in
        postgres | pg_* | public | none) fail "$1 uses a reserved role name" ;;
    esac
}

check_password() {
    eval "value=\${$1}"
    [ "${#value}" -ge 24 ] || fail "$1 must have at least 24 characters"
    lowered=$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]')
    case "$lowered" in
        *change* | *placeholder* | *example* | *password* | *secret* | *replace* | *xxxx* | *dev-* | *test-*)
            fail "$1 looks like a placeholder value"
            ;;
    esac
    case "$value" in
        *"
"*) fail "$1 must be a single line" ;;
    esac
}

for var in POSTGRES_DB POSTGRES_USER \
    GMAT_DB_OWNER_USER GMAT_DB_OWNER_PASSWORD \
    GMAT_DB_RUNTIME_USER GMAT_DB_RUNTIME_PASSWORD \
    GMAT_DB_BACKUP_USER GMAT_DB_BACKUP_PASSWORD; do
    require "$var"
done

check_name GMAT_DB_OWNER_USER
check_name GMAT_DB_RUNTIME_USER
check_name GMAT_DB_BACKUP_USER
check_password GMAT_DB_OWNER_PASSWORD
check_password GMAT_DB_RUNTIME_PASSWORD
check_password GMAT_DB_BACKUP_PASSWORD

# One role, one purpose, one secret.
[ "$GMAT_DB_OWNER_USER" != "$GMAT_DB_RUNTIME_USER" ] || fail "owner and runtime roles must differ"
[ "$GMAT_DB_OWNER_USER" != "$GMAT_DB_BACKUP_USER" ] || fail "owner and backup roles must differ"
[ "$GMAT_DB_RUNTIME_USER" != "$GMAT_DB_BACKUP_USER" ] || fail "runtime and backup roles must differ"
[ "$GMAT_DB_OWNER_PASSWORD" != "$GMAT_DB_RUNTIME_PASSWORD" ] || fail "role passwords must differ"
[ "$GMAT_DB_OWNER_PASSWORD" != "$GMAT_DB_BACKUP_PASSWORD" ] || fail "role passwords must differ"
[ "$GMAT_DB_RUNTIME_PASSWORD" != "$GMAT_DB_BACKUP_PASSWORD" ] || fail "role passwords must differ"
if [ -n "${POSTGRES_PASSWORD:-}" ]; then
    for pw in "$GMAT_DB_OWNER_PASSWORD" "$GMAT_DB_RUNTIME_PASSWORD" "$GMAT_DB_BACKUP_PASSWORD"; do
        [ "$pw" != "$POSTGRES_PASSWORD" ] || fail "a role password equals the bootstrap superuser's"
    done
fi

# The passwords travel as psql variables: never interpolated into the shell command line
# or the SQL text by hand, and never echoed (-q, no ON_ERROR_STOP trace of values).
psql -v ON_ERROR_STOP=1 -q --no-psqlrc \
    --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    -v db="$POSTGRES_DB" \
    -v owner="$GMAT_DB_OWNER_USER" -v owner_pw="$GMAT_DB_OWNER_PASSWORD" \
    -v runtime="$GMAT_DB_RUNTIME_USER" -v runtime_pw="$GMAT_DB_RUNTIME_PASSWORD" \
    -v backup="$GMAT_DB_BACKUP_USER" -v backup_pw="$GMAT_DB_BACKUP_PASSWORD" <<'SQL'
SET password_encryption = 'scram-sha-256';

-- Roles: login only, no superuser, no role or database creation, no replication.
SELECT format('CREATE ROLE %I', :'owner')
  WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'owner') \gexec
SELECT format('CREATE ROLE %I', :'runtime')
  WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'runtime') \gexec
SELECT format('CREATE ROLE %I', :'backup')
  WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'backup') \gexec

SELECT format('ALTER ROLE %I WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L',
              :'owner', :'owner_pw') \gexec
SELECT format('ALTER ROLE %I WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L',
              :'runtime', :'runtime_pw') \gexec
SELECT format('ALTER ROLE %I WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L',
              :'backup', :'backup_pw') \gexec

-- A stuck client must not hold locks forever (the application also sets lock_timeout).
SELECT format('ALTER ROLE %I SET idle_in_transaction_session_timeout = %L', :'runtime', '60s') \gexec
SELECT format('ALTER ROLE %I SET default_transaction_read_only = on', :'backup') \gexec

-- Database: nobody connects by default; the owner owns it and the schema.
REVOKE ALL ON DATABASE :"db" FROM PUBLIC;
ALTER DATABASE :"db" OWNER TO :"owner";
GRANT CONNECT ON DATABASE :"db" TO :"runtime", :"backup";
ALTER SCHEMA public OWNER TO :"owner";
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO :"runtime", :"backup";

-- Runtime: data access only, on what the owner creates now and later.
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner" IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"runtime";
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner" IN SCHEMA public
    GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO :"runtime";
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO :"runtime";
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO :"runtime";

-- Backup: read everything, write nothing.
GRANT pg_read_all_data TO :"backup";
SQL

echo "10-roles.sh: owner, runtime and backup roles are in place" >&2
