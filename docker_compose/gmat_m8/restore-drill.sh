#!/usr/bin/env bash
# The quarterly offsite restore drill (plan §9, verification level 3; F19, F34, F67, F75).
#
# On an isolated machine with an encrypted disk, with a checkout of this repository and offline
# copies of the env files, and no access to production. The restored data holds personal data:
# wipe it afterwards (--wipe, or `docker compose ... down --volumes` and the files).
#
#   ./restore-drill.sh --dump BASE --app-image REF --backup-image REF --operator EMAIL
#                      [--from-dir DIR | --snapshot ID] [--preserved-dump FILE] [--wipe]
#
#   --dump BASE       the dump's base name, gmat-<stamp>[-<label>]; "--snapshot" finds it
#   --from-dir DIR    the dump, its manifest and .media/ fetched by hand with restic
#   --snapshot ID     fetch it now with restic in the backup image (needs RESTIC_* in the
#                     environment or .env and, for this one step, a route to the repository)
#   --app-image REF   the app image by digest, the same digest production runs
#   --backup-image REF  the backup image by digest
#   --preserved-dump FILE  a later dump standing for the failed database: its audit log is
#                     replayed. Without it, privileged accounts must come up suspended.
#
# Runs on its own project (gmat-m8-restore-drill) with every network internal and
# OUTBOUND_EMAIL=off, then checks what the plan lists and prints the line for
# docs/runbooks/drill-log.md. Exit 0 only if every check passed.
# shellcheck disable=SC2016  # the single-quoted commands expand inside the containers
# shellcheck disable=SC2015  # `a && pass || fail` reports either way
set -Eeuo pipefail
umask 077
cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"

export DEPLOY_COMPOSE_FILES="${DRILL_COMPOSE_FILES:-docker-compose.yml:docker-compose.restore-drill.yml}"
export COMPOSE_PROJECT_NAME="${DRILL_PROJECT_NAME:-gmat-m8-restore-drill}"
# shellcheck source=ops/common.sh
. ops/common.sh

usage() {
    sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//' >&2
    exit 64
}

DUMP=""
FROM_DIR=""
SNAPSHOT=""
OPERATOR=""
PRESERVED_DUMP=""
WIPE=0
APP_IMAGE=""
BACKUP_IMAGE=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --dump) DUMP="${2:?}"; shift 2 ;;
        --from-dir) FROM_DIR="${2:?}"; shift 2 ;;
        --snapshot) SNAPSHOT="${2:?}"; shift 2 ;;
        --operator) OPERATOR="${2:?}"; shift 2 ;;
        --preserved-dump) PRESERVED_DUMP="${2:?}"; shift 2 ;;
        --app-image) APP_IMAGE="${2:?}"; shift 2 ;;
        --backup-image) BACKUP_IMAGE="${2:?}"; shift 2 ;;
        --wipe) WIPE=1; shift ;;
        *) usage ;;
    esac
done
[ -n "$OPERATOR" ] && [ -n "$APP_IMAGE" ] && [ -n "$BACKUP_IMAGE" ] || usage
[ -n "$DUMP" ] || [ -n "$SNAPSHOT" ] || usage
[ ! -f "$DEPLOYED_LOG" ] || die "this directory has run a deploy: a drill never runs next to production"
export APP_IMAGE BACKUP_IMAGE

FAILURES=0
pass() { printf 'PASS  %s\n' "$1"; }
fail() {
    printf 'FAIL  %s\n' "$1"
    FAILURES=$((FAILURES + 1))
}

sql_value() { printf '%s\n' "$1" | db_sql -t -A; }

expect_value() { # expect_value TEXT SQL EXPECTED
    local found
    found=$(sql_value "$2")
    if [ "$found" = "$3" ]; then pass "$1"; else fail "$1 (found $found, expected $3)"; fi
}

fetch_files() {
    mkdir -p backups
    if [ -n "$SNAPSHOT" ]; then
        docker run --rm --network "${DRILL_RESTIC_NETWORK:-bridge}" --entrypoint restic \
            --user "$(id -u):$(id -g)" -e RESTIC_REPOSITORY -e RESTIC_PASSWORD -e RESTIC_REST_USERNAME \
            -e RESTIC_REST_PASSWORD -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY -e AWS_DEFAULT_REGION \
            -e RESTIC_CACHE_DIR=/tmp/restic-cache \
            -v "$PWD/backups:/backups" "$BACKUP_IMAGE" restore "$SNAPSHOT" --target /backups/.offsite
        local found
        found=$(find backups/.offsite/backups -maxdepth 1 -name 'gmat-*.dump' -print -quit)
        [ -n "$found" ] || die "snapshot $SNAPSHOT holds no dump"
        DUMP=$(basename "$found" .dump)
        mv backups/.offsite/backups/"$DUMP"* backups/
        rm -rf backups/.offsite
    elif [ -n "$FROM_DIR" ]; then
        cp -a "$FROM_DIR/$DUMP".* backups/
    fi
    [ -f "backups/$DUMP.dump" ] && [ -f "backups/$DUMP.manifest.json" ] \
        || die "backups/$DUMP.dump and its manifest are needed"
}

# A later dump stands for the failed database: restored aside, read with the same export a real
# recovery uses, and dropped.
failed_state_from_dump() { # failed_state_from_dump FILE OUTPUT
    compose exec -T db sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" exec psql -X -q -v ON_ERROR_STOP=1 \
        -h 127.0.0.1 -U "$POSTGRES_USER" -d postgres' <<'SQL'
DROP DATABASE IF EXISTS gmat_drill_failed WITH (FORCE);
CREATE DATABASE gmat_drill_failed TEMPLATE template0 ENCODING 'UTF8';
SQL
    compose exec -T db sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" exec pg_restore -h 127.0.0.1 \
        -U "$POSTGRES_USER" -d gmat_drill_failed --no-owner --no-privileges --exit-on-error' <"$1"
    compose exec -T db sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" exec psql -X -q -t -A \
        -v ON_ERROR_STOP=1 -h 127.0.0.1 -U "$POSTGRES_USER" -d gmat_drill_failed' \
        <ops/failed-state.sql >"$2"
    printf 'DROP DATABASE gmat_drill_failed WITH (FORCE);\n' | compose exec -T db sh -c \
        'PGPASSWORD="$POSTGRES_PASSWORD" exec psql -X -q -h 127.0.0.1 -U "$POSTGRES_USER" -d postgres'
}

check_counts() {
    local restored="drill/restored.json"
    db_sql -t -A -v audit_table=audit_auditevent <../../docker/backup/manifest.sql >"$restored"
    if python3 ops/checks.py compare-counts "backups/$DUMP.manifest.json" "$restored"; then
        pass "row counts match the manifest"
    else
        fail "row counts match the manifest"
    fi
}

check_runtime_role() {
    if compose exec -T db sh -c 'PGPASSWORD="$GMAT_DB_RUNTIME_PASSWORD" exec psql -X -q \
        -v ON_ERROR_STOP=1 -h 127.0.0.1 -U "$GMAT_DB_RUNTIME_USER" -d "$POSTGRES_DB"' <<'SQL'
BEGIN;
INSERT INTO django_cache (cache_key, value, expires) VALUES ('drill', 'x', now());
ROLLBACK;
SQL
    then pass "the runtime role connects and can write"; else fail "the runtime role connects and can write"; fi
}

check_audit_trigger() { # check_audit_trigger STATEMENT
    local output
    if output=$(printf '%s\n' "BEGIN; $1; ROLLBACK;" | compose exec -T db sh -c \
        'PGPASSWORD="$GMAT_DB_RUNTIME_PASSWORD" exec psql -X -q -v ON_ERROR_STOP=1 -h 127.0.0.1 \
        -U "$GMAT_DB_RUNTIME_USER" -d "$POSTGRES_DB"' 2>&1); then
        fail "the audit trigger blocks: $1"
    elif grep -qiE 'append|immutable|restrict|not allowed' <<<"$output"; then
        pass "the audit trigger blocks: $1"
    else
        fail "the audit trigger blocks: $1 ($output)"
    fi
}

check_attachments() {
    local checksum
    checksum='
import hashlib, json, pathlib, sys
rows = json.load(sys.stdin).get("attachments") or []
bad = [r["storage_key"] for r in rows[:20]
       if hashlib.sha256(pathlib.Path("/srv/media/files", r["storage_key"]).read_bytes()).hexdigest() != r["sha256"]]
print(f"{len(rows[:20])} checked")
sys.exit(1 if bad else 0)
'
    if compose run --rm --no-deps -T --entrypoint python web -c "$checksum" \
        <"backups/$DUMP.manifest.json"; then
        pass "a sample of attachments opens and matches its checksum"
    else
        fail "a sample of attachments opens and matches its checksum"
    fi
}

check_no_way_out() { # check_no_way_out SERVICE
    local probe
    probe='
import socket, sys
for host, port in (("1.1.1.1", 443), ("1.1.1.1", 587), ("smtp.gmail.com", 587)):
    try:
        socket.create_connection((host, port), timeout=5).close()
    except OSError:
        continue
    print(f"reached {host}:{port}")
    sys.exit(1)
'
    if compose exec -T "$1" python -c "$probe"; then
        pass "$1: outbound HTTP and SMTP fail"
    else
        fail "$1: outbound HTTP and SMTP fail"
    fi
}

check_reconciled() { # check_reconciled ATTEMPTS_BEFORE
    expect_value "no session row left" "SELECT count(*) FROM django_session;" 0
    expect_value "no row left in the session list" "SELECT count(*) FROM usersessions_usersession;" 0
    expect_value "no pending invitation" \
        "SELECT count(*) FROM accounts_invitation WHERE accepted_at IS NULL AND revoked_at IS NULL;" 0
    expect_value "no open email change" \
        "SELECT count(*) FROM accounts_emailchangerequest WHERE applied_at IS NULL AND cancelled_at IS NULL AND revoked_at IS NULL;" 0
    expect_value "no recovery code left" \
        "SELECT count(*) FROM mfa_authenticator WHERE type = 'recovery_codes';" 0
    expect_value "no unsent security email" \
        "SELECT count(*) FROM notifications_emailoutbox WHERE sensitive AND status IN ('QUEUED','SENDING','FAILED','AMBIGUOUS','QUARANTINED');" 0
    expect_value "nothing queued: restored messages are held" \
        "SELECT count(*) FROM notifications_emailoutbox WHERE status IN ('QUEUED','SENDING','FAILED','AMBIGUOUS');" 0
    expect_value "nothing was sent: no new attempt" \
        "SELECT count(*) FROM notifications_emailattempt;" "$1"
    if [ -n "$PRESERVED_DUMP" ]; then
        expect_value "the failed state was replayed" \
            "SELECT count(*) > 0 FROM audit_auditevent WHERE action = 'recovery.reconciled' AND (data->>'failed_state_used')::boolean;" t
    else
        expect_value "privileged accounts come up suspended" \
            "SELECT count(*) > 0 FROM audit_auditevent WHERE action = 'recovery.account_suspended';" t
    fi
    local held
    held=$(sql_value "SELECT count(*) FROM notifications_emailoutbox WHERE status = 'QUARANTINED';")
    printf 'INFO  %s message(s) held in quarantine, nothing sent\n' "$held"
}

main() {
    take_lock
    mkdir -p drill
    fetch_files
    gates_on
    compose up -d --no-deps --wait db
    verify_dump "$DUMP"
    pass "the dump's checksum and contents match its manifest"
    restore_database "backups/$DUMP.dump"
    restore_media "$DUMP"
    check_counts

    local attempts secret_before secret_after
    attempts=$(sql_value "SELECT count(*) FROM notifications_emailattempt;")
    secret_before=$(env_value DJANGO_SECRET_KEY app.env | sha256sum)
    rotate_secret_key
    secret_after=$(env_value DJANGO_SECRET_KEY app.env | sha256sum)
    [ "$secret_before" != "$secret_after" ] && pass "SECRET_KEY rotated" || fail "SECRET_KEY rotated"

    if [ -n "$PRESERVED_DUMP" ]; then
        failed_state_from_dump "$PRESERVED_DUMP" drill/failed-state.json
        reconcile_from_state "$OPERATOR" drill/failed-state.json drill/reconcile-report.json
    else
        reconcile_without_state "$OPERATOR" drill/reconcile-report.json
    fi
    pass "post-restore-reconcile ran"

    compose up -d --no-deps --no-build --wait --wait-timeout 300 web worker
    wait_worker_state paused 90 && pass "the worker is up and paused" || fail "the worker is up and paused"
    check_runtime_role
    check_audit_trigger "UPDATE audit_auditevent SET action = 'x.y'"
    check_audit_trigger "DELETE FROM audit_auditevent"
    check_attachments
    check_reconciled "$attempts"
    check_no_way_out web
    check_no_way_out worker

    local verdict=PASSED
    [ "$FAILURES" -eq 0 ] || verdict="FAILED ($FAILURES)"
    printf '\nFor docs/runbooks/drill-log.md:\n| %s | %s | %s | %s |\n' \
        "$(date -u +%Y-%m-%d)" "$DUMP" "${APP_IMAGE##*@}" "$verdict"
    if [ "$WIPE" = 1 ]; then
        compose down --volumes --remove-orphans
        rm -rf backups/gmat-* drill flags/maintenance.on flags/worker.paused
        say "the drill's containers, volumes and restored files are wiped"
    else
        say "the restored data holds personal data: wipe it (--wipe) when the drill is over"
    fi
    [ "$FAILURES" -eq 0 ]
}

main
