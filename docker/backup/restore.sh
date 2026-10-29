#!/usr/bin/env bash
# Proof that a dump restores and matches its manifest (plan §9, verification level 2).
#
#   restore.sh --verify [DUMP]    restore into a private scratch cluster and compare
#   restore.sh --list             list the dumps in $BACKUP_DIR, newest first
#
# DUMP is a file name or path of a gmat-*.dump; the newest one is used by default.
#
# The scratch cluster is a throw-away PostgreSQL started inside this container: it listens
# on a unix socket in $BACKUP_SCRATCH_DIR only, so it needs no network and no write right on
# the real database (the backup role is read-only), and it works unchanged on an isolated
# machine during the offsite restore drill. It is wiped before the script ends.
#
# Checks: the dump's sha256 against the manifest; pg_restore completes without error; every
# table's row count equals the manifest (the excluded tables must be empty); the migration
# head and the newest audit event match.
set -Eeuo pipefail
umask 077
# shellcheck source=common.sh
. "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/common.sh"

usage() {
    echo "usage: restore.sh {--verify [DUMP]|--list}" >&2
    exit 64
}

list_dumps() {
    # shellcheck disable=SC2012
    ls -1t "$BACKUP_DIR"/gmat-*.dump 2>/dev/null || true
}

SCRATCH=""
cleanup() {
    local status=$?
    if [ -n "$SCRATCH" ] && [ -d "$SCRATCH" ]; then
        pg_ctl -D "$SCRATCH/data" -m immediate -w stop >/dev/null 2>&1 || true
        rm -rf "$SCRATCH"
    fi
    exit "$status"
}

verify() {
    local dump="${1:-}"
    if [ -z "$dump" ]; then
        dump=$(list_dumps | head -n 1)
        [ -n "$dump" ] || die "no dump found in $BACKUP_DIR"
    elif [ ! -f "$dump" ]; then
        dump="$BACKUP_DIR/$dump"
    fi
    [ -f "$dump" ] || die "dump not found: $dump"
    local manifest="${dump%.dump}.manifest.json"
    [ -f "$manifest" ] || die "manifest not found: $manifest"
    jq -e '.format_version == 1' "$manifest" >/dev/null || die "unsupported manifest format"

    log "verifying $(basename "$dump")"
    local expected actual
    expected=$(jq -r '.dump.sha256' "$manifest")
    actual=$(sha256_of "$dump")
    [ "$expected" = "$actual" ] || die "dump checksum differs from its manifest"

    # --- scratch cluster ----------------------------------------------------------------
    SCRATCH=$(mktemp -d "$BACKUP_SCRATCH_DIR/verify.XXXXXX")
    trap cleanup EXIT
    local sock="$SCRATCH/sock"
    mkdir -p "$sock"
    initdb -D "$SCRATCH/data" -U verify --auth=trust --locale=C --encoding=UTF8 --no-instructions >/dev/null
    pg_ctl -D "$SCRATCH/data" -w -l "$SCRATCH/server.log" \
        -o "-c listen_addresses= -c unix_socket_directories=$sock -c fsync=off -c synchronous_commit=off -c full_page_writes=off" \
        start >/dev/null || { tail -n 20 "$SCRATCH/server.log" >&2; die "scratch server did not start"; }

    # The environment points at the real database; the scratch cluster is addressed explicitly.
    unset PGPASSWORD PGHOST PGPORT PGDATABASE PGUSER
    export PGHOST="$sock" PGUSER=verify
    createdb verify
    pg_restore --no-owner --no-privileges --exit-on-error --dbname=verify "$dump" \
        || die "pg_restore failed"

    # --- compare with the manifest ------------------------------------------------------
    local restored
    restored=$(psql -X -q -t -A -v ON_ERROR_STOP=1 -d verify \
        -v "audit_table=${AUDIT_TABLE:-audit_auditevent}" -f "$MANIFEST_SQL")
    local problems
    problems=$(jq -n --slurpfile m "$manifest" --argjson r "$restored" '
        $m[0] as $m
        | [ ($m.row_counts | to_entries[] | select(.key as $t | ($m.excluded_data_tables | index($t)) == null)
              | select(($r.row_counts[.key] // -1) != .value)
              | "rows of \(.key): manifest \(.value), restored \($r.row_counts[.key] // "missing")"),
            ($m.excluded_data_tables[] | select(($r.row_counts[.] // 0) != 0)
              | "\(.) must be empty after a restore, found \($r.row_counts[.])"),
            ($r.row_counts | keys[] | select(. as $t | $m.row_counts | has($t) | not)
              | "table \(.) is not in the manifest"),
            (if $m.migration_head != $r.migration_head then "migration head differs" else empty end),
            (if $m.audit_last_event_id != $r.audit_last_event_id then "newest audit event differs" else empty end)
          ] | .[]' -r)
    if [ -n "$problems" ]; then
        printf '%s\n' "$problems" >&2
        die "restore of $(basename "$dump") does not match its manifest"
    fi
    touch "$BACKUP_DIR/.last-verify-ok"
    log "restore verify OK: $(basename "$dump") matches its manifest"
}

case "${1:-}" in
    --verify)
        shift
        verify "$@"
        ;;
    --list) list_dumps ;;
    *) usage ;;
esac
