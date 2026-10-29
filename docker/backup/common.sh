#!/usr/bin/env bash
# Shared helpers for backup.sh and restore.sh. Sourced, never executed.
# shellcheck disable=SC2034  # the variables below are used by the scripts that source this file

: "${BACKUP_DIR:=/backups}"
: "${BACKUP_SCRATCH_DIR:=/scratch}"
: "${BACKUP_KEEP_LOCAL_DAYS:=3}"
LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFEST_SQL="$LIB_DIR/manifest.sql"

# Tables whose rows are deliberately left out of every dump (plan §9): live session keys and
# rate-limit counters must not travel in a backup.
EXCLUDED_DATA_TABLES=(django_session django_cache)

# One bigint key for the media/backup coordination lock. The worker's housekeeping takes the
# same key with pg_try_advisory_lock before purging soft-deleted media (plan §9).
MEDIA_LOCK_SQL="hashtextextended('gmat:media-backup', 0)"

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

require_env() {
    local name
    for name in "$@"; do
        [ -n "${!name:-}" ] || die "$name is required"
    done
}

# psql through a long-lived coprocess, so one session can hold the advisory lock and the
# exported snapshot while pg_dump runs. `session_start`, `session_run <sql>`, `session_end`.
# bash closes coprocess descriptors in subshells, so `session_run` must be called in the
# main shell (never in `$(...)` or a pipeline); its output is left in SESSION_OUT.
session_start() {
    coproc PSQL { psql -X -q -t -A -v ON_ERROR_STOP=1 -d "${PGDATABASE}" 2>&1; }
    SESSION_OPEN=1
}

SESSION_OUT=""
session_run() {
    local sql="$1" line
    SESSION_OUT=""
    printf '%s\n\\echo __SESSION_END__\n' "$sql" >&"${PSQL[1]}" || die "database session is gone"
    while IFS= read -r -u "${PSQL[0]}" line; do
        [ "$line" = "__SESSION_END__" ] && return 0
        SESSION_OUT+="$line"$'\n'
    done
    die "database session ended unexpectedly: ${SESSION_OUT:-no output}"
}

session_end() {
    if [ "${SESSION_OPEN:-0}" = 1 ]; then
        SESSION_OPEN=0
        printf '\\q\n' >&"${PSQL[1]}" 2>/dev/null || true
        wait "$PSQL_PID" 2>/dev/null || true
    fi
}

sha256_of() { sha256sum "$1" | cut -d' ' -f1; }
