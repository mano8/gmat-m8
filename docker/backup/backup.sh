#!/usr/bin/env bash
# Backup of the gmat-m8 database (plan §9, "Backups and recovery points").
#
#   backup.sh daemon                 run the scheduler (the container's default command)
#   backup.sh run [--label LABEL]    one backup now        (also: backup-now [--label LABEL])
#
# One run:
#   1. a psql session takes the advisory lock `gmat:media-backup`, opens a REPEATABLE READ
#      READ ONLY transaction and exports its snapshot; the session stays open;
#   2. pg_dump -Fc --snapshot=<id>, sessions and the rate-limit cache excluded (data only);
#   3. the manifest queries run in that same snapshot, so dump and manifest always agree;
#   4. the media directory, if any, is copied under the lock and checksummed;
#   5. the transaction ends and the lock is released;
#   then: pg_restore --list, local retention, the append-only offsite upload and the heartbeat.
#
# Files in $BACKUP_DIR (mode 0600, directory 0700):
#   gmat-<UTC timestamp>[-<label>].dump | .manifest.json | .media/
set -Eeuo pipefail
umask 077
# shellcheck source=common.sh
. "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/common.sh"

usage() {
    echo "usage: backup.sh {daemon|run [--label LABEL]|backup-now [--label LABEL]}" >&2
    exit 64
}

daemon() {
    require_env PGHOST PGDATABASE PGUSER PGPASSWORD
    log "backup daemon started (supercronic)"
    # supercronic hands the container environment to its jobs.
    exec supercronic /etc/gmat-backup.crontab
}

cleanup() {
    local status=$?
    session_end
    if [ -n "${PARTIAL_FILES:-}" ]; then
        # shellcheck disable=SC2086
        rm -rf $PARTIAL_FILES
    fi
    if [ "$status" -ne 0 ]; then
        heartbeat fail
    fi
}

run_backup() {
    require_env PGHOST PGDATABASE PGUSER PGPASSWORD
    local label=""
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --label)
                [ "$#" -ge 2 ] || usage
                label="$2"
                shift 2
                ;;
            *) usage ;;
        esac
    done
    if [ -n "$label" ] && ! [[ "$label" =~ ^[A-Za-z0-9._-]{1,40}$ ]]; then
        die "--label may only contain letters, digits, '.', '_' and '-' (max 40)"
    fi

    local started stamp base dump manifest media
    started=$(date +%s)
    stamp=$(date -u +%Y%m%dT%H%M%SZ)
    base="gmat-${stamp}${label:+-$label}"
    dump="$BACKUP_DIR/$base.dump"
    manifest="$BACKUP_DIR/$base.manifest.json"
    media="$BACKUP_DIR/$base.media"

    mkdir -p "$BACKUP_DIR"
    # Files are 0600 whatever the directory mode; the directory itself should be 0700 and
    # owned by uid 1000 (init.sh prepares it). A bind mount owned by someone else only warns.
    if [ "$(stat -c %a "$BACKUP_DIR")" != 700 ]; then
        chmod 0700 "$BACKUP_DIR" 2>/dev/null \
            || log "WARNING: $BACKUP_DIR is not mode 0700 and cannot be changed by uid $(id -u)"
    fi
    # Two backups never overlap (cron against a pre-deploy run).
    exec 9>"$BACKUP_DIR/.backup.lock"
    flock -n 9 || die "another backup is already running"

    PARTIAL_FILES="$dump.partial $manifest.partial $media.partial"
    trap cleanup EXIT

    # --- 1. lock + snapshot -------------------------------------------------------------
    session_start
    session_run "SET lock_timeout = '60s';"
    session_run "SELECT pg_advisory_lock($MEDIA_LOCK_SQL);"
    session_run "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;"
    session_run "SELECT pg_export_snapshot();"
    local snapshot="${SESSION_OUT//[[:space:]]/}"
    [[ "$snapshot" =~ ^[0-9A-Fa-f-]+$ ]] || die "could not export a snapshot"
    log "snapshot $snapshot exported, advisory lock held"

    # --- 2. dump ------------------------------------------------------------------------
    local exclude=() table
    for table in "${EXCLUDED_DATA_TABLES[@]}"; do
        exclude+=("--exclude-table-data=$table")
    done
    pg_dump --format=custom --snapshot="$snapshot" "${exclude[@]}" \
        --file="$dump.partial" "$PGDATABASE"

    # --- 3. manifest, in the same snapshot ----------------------------------------------
    session_run "\\set audit_table '${AUDIT_TABLE:-audit_auditevent}'"
    session_run "$(cat "$MANIFEST_SQL")"
    local data="$SESSION_OUT"
    jq -e . >/dev/null <<<"$data" || die "the manifest query returned no valid JSON"

    # --- 4. media (until M6.4 there is normally none) -----------------------------------
    local media_json='null'
    if [ -n "${MEDIA_DIR:-}" ] && [ -d "$MEDIA_DIR" ] && [ -n "$(ls -A "$MEDIA_DIR" 2>/dev/null)" ]; then
        mkdir -p "$media.partial"
        cp -a "$MEDIA_DIR"/. "$media.partial"/
        local listing count
        listing=$(cd "$media.partial" && find . -type f -print0 | sort -z | xargs -0 -r sha256sum)
        count=$(printf '%s' "$listing" | grep -c . || true)
        media_json=$(jq -n --arg dir "$base.media" --argjson files "$count" \
            --arg digest "$(printf '%s' "$listing" | sha256sum | cut -d' ' -f1)" \
            '{directory: $dir, file_count: $files, listing_sha256: $digest}')
    fi

    # --- 5. release the snapshot and the lock -------------------------------------------
    session_run "COMMIT;"
    session_end
    log "snapshot released"

    # --- finalize -----------------------------------------------------------------------
    pg_restore --list "$dump.partial" >/dev/null || die "pg_restore cannot read the dump"
    local dump_sha dump_bytes
    dump_sha=$(sha256_of "$dump.partial")
    dump_bytes=$(stat -c %s "$dump.partial")
    jq -n --argjson data "$data" --argjson media "$media_json" \
        --arg dump "$base.dump" --arg sha "$dump_sha" --argjson bytes "$dump_bytes" \
        --arg created "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --arg label "$label" \
        --arg version "${APP_VERSION:-unknown}" --arg database "$PGDATABASE" \
        --arg snapshot "$snapshot" \
        --argjson excluded "$(printf '%s\n' "${EXCLUDED_DATA_TABLES[@]}" | jq -R . | jq -s .)" \
        '{format_version: 1, created_at: $created, label: (if $label == "" then null else $label end),
          app_version: $version, database: $database, snapshot: $snapshot,
          dump: {file: $dump, sha256: $sha, bytes: $bytes},
          excluded_data_tables: $excluded, media: $media} + $data' >"$manifest.partial"
    mv "$dump.partial" "$dump"
    [ ! -d "$media.partial" ] || mv "$media.partial" "$media"
    mv "$manifest.partial" "$manifest"
    PARTIAL_FILES=""
    log "dump $base.dump ($dump_bytes bytes) written with its manifest"

    # --- local retention ----------------------------------------------------------------
    find "$BACKUP_DIR" -maxdepth 1 -name 'gmat-*' -mtime "+$BACKUP_KEEP_LOCAL_DAYS" \
        -exec rm -rf {} + 2>/dev/null || true

    # --- offsite, append-only -----------------------------------------------------------
    offsite_upload "$base" "$dump" "$manifest" "$media" "$label"

    touch "$BACKUP_DIR/.last-success"
    heartbeat ""
    log "backup $base finished in $(($(date +%s) - started)) s"
    echo "$base"
}

# restic with credentials that can append but not delete or overwrite (plan §9). Forget and
# prune run only from the maintainer's machine, never here. Fixed host and tags let the
# maintainer's pre-prune check recognise legitimate snapshots.
offsite_upload() {
    local base="$1" dump="$2" manifest="$3" media="$4" label="$5"
    if [ -z "${RESTIC_REPOSITORY:-}" ]; then
        log "offsite upload skipped: RESTIC_REPOSITORY is not set"
        return 0
    fi
    require_env RESTIC_PASSWORD
    local paths=("$dump" "$manifest") tags=(--tag gmat-m8 --tag "dump:$base")
    [ ! -d "$media" ] || paths+=("$media")
    [ -z "$label" ] || tags+=(--tag "label:$label")
    if ! restic cat config >/dev/null 2>&1; then
        restic init >/dev/null || die "restic could not initialise the repository"
    fi
    restic backup --host gmat-m8 --quiet "${tags[@]}" "${paths[@]}" \
        || die "restic upload failed"
    local found
    found=$(restic snapshots --host gmat-m8 --tag "dump:$base" --json | jq 'length')
    [ "$found" -ge 1 ] || die "the offsite snapshot for $base is not listed"
    log "offsite snapshot for $base verified"
}

heartbeat() {
    [ -n "${BACKUP_HEARTBEAT_URL:-}" ] || return 0
    curl -fsS -m 10 --retry 2 -o /dev/null "${BACKUP_HEARTBEAT_URL%/}${1:+/$1}" \
        || log "heartbeat ping failed"
}

# Invoked through the `backup-now` link: `backup-now --label X` is `backup.sh run --label X`.
if [ "$(basename "$0")" = "backup-now" ]; then
    set -- run "$@"
fi

case "${1:-daemon}" in
    daemon) daemon ;;
    run | backup-now)
        shift
        run_backup "$@"
        ;;
    *) usage ;;
esac
