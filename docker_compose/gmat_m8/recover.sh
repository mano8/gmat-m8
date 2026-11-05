#!/usr/bin/env bash
# Recovery (plan §9, F23, F24, F43, F44): the human decides, this executes.
#
#   ./recover.sh status
#       where the last deploy stopped and the matching row of the decision tree.
#   ./recover.sh rollback
#       failed before step 7: start the previous digests. No data loss.
#   ./recover.sh migrate-back
#       failed at steps 7-9, a reversible release: migrate back to the previous head with the new
#       image, then start the previous digests. No data loss.
#   ./recover.sh restore-predeploy --operator EMAIL [--confirm WORD]
#       failed at steps 7-9, otherwise: preserve the failed state, restore the pre-deploy dump,
#       reconcile, start the previous digests. No data loss: nothing was written since step 5.
#   ./recover.sh rollback-images
#       a defect after the commit point, and the notes say the old code works on the new schema.
#       Forward-fix first (./deploy.sh X.Y.Z+1).
#   ./recover.sh restore DUMP --operator EMAIL --incident TEXT [--version V] [--db-unreadable]
#                             [--confirm WORD]
#       only a restore can fix it and writes were accepted: an explicit decision. Preserves the
#       failed state and what was written since the deploy, states the loss window, asks.
#   ./recover.sh restore-offsite --snapshot ID --version V --operator EMAIL --incident TEXT
#                                [--gap-offboarded FILE] [--gap-credentials-changed FILE]
#                                [--confirm WORD]
#       the VPS is gone, on the new one: no failed state, so every privileged account comes up
#       suspended (Q23). FILEs list one address per line. Loss: up to the RPO (6 h).
#
# Every action but `status` takes the deploy lock first (a recovery that waited for a deploy
# then reads how that deploy ended), puts both gates on, stops the writers, and ends with the
# internal verification and the commit point. A failure leaves both gates on.
# shellcheck disable=SC2016  # the single-quoted commands expand inside the db container
# shellcheck disable=SC2015  # `a && b || die` stops either way
set -Eeuo pipefail
umask 077
cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
# shellcheck source=ops/common.sh
. ops/common.sh

usage() {
    sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//' >&2
    exit 64
}

OPERATOR=""
CONFIRM=""
INCIDENT=""
RESTORE_VERSION=""
SNAPSHOT=""
DB_UNREADABLE=0
DUMP_ARG=""
GAP_ARGS=()

append_gap() { # append_gap OPTION FILE
    local address
    [ -f "$2" ] || die "$2 does not exist"
    while IFS= read -r address || [ -n "$address" ]; do
        [ -z "$address" ] || GAP_ARGS+=("$1" "$address")
    done <"$2"
}

parse_options() {
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --operator) OPERATOR="${2:?}"; shift 2 ;;
            --confirm) CONFIRM="${2:?}"; shift 2 ;;
            --incident) INCIDENT="${2:?}"; shift 2 ;;
            --version) RESTORE_VERSION="${2:?}"; shift 2 ;;
            --snapshot) SNAPSHOT="${2:?}"; shift 2 ;;
            --db-unreadable) DB_UNREADABLE=1; shift ;;
            --gap-offboarded) append_gap --gap-offboarded "${2:?}"; shift 2 ;;
            --gap-credentials-changed) append_gap --gap-credentials-changed "${2:?}"; shift 2 ;;
            -*) usage ;;
            *)
                [ -z "$DUMP_ARG" ] || usage
                DUMP_ARG="$1"
                shift
                ;;
        esac
    done
}

require_operator() { [ -n "$OPERATOR" ] || die "--operator EMAIL is required: a platform operator"; }

begin() {
    gates_on
    service_running caddy || compose up -d --no-deps caddy
    stop_writers
    compose stop backup
    service_running db || compose up -d --no-deps --wait db
}

# Starts the digests chosen, verifies and opens. KIND goes to deployed.log.
finish() { # finish VERSION APP BACKUP DUMP KIND
    use_digests "$1" "$2" "$3"
    start_services
    verify_internal
    commit_point
    record_deployed "$1" "$2" "$3" "$4" "$5"
    state_set STATUS "done"
    state_set RECOVERED_AT "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    rm -f deploy.head
    say "recovered: $1 is running ($5)"
}

previous_or_die() {
    PREVIOUS_VERSION=$(state_get PREVIOUS_VERSION)
    PREVIOUS_APP=$(state_get PREVIOUS_APP_DIGEST)
    PREVIOUS_BACKUP=$(state_get PREVIOUS_BACKUP_DIGEST)
    [ -n "$PREVIOUS_APP" ] && [ -n "$PREVIOUS_BACKUP" ] \
        || die "no previous release is recorded: this was the first deploy. Fix it and deploy again."
}

require_unfinished_deploy() {
    [ "$(state_get STATUS)" = failed ] || [ "$(state_get STATUS)" = running ] \
        || die "no failed deploy is recorded (see ./recover.sh status)"
    [ "$(reached_step)" -ge 4 ] || die "that deploy stopped before the gates: nothing changed"
}

reached_step() { state_get STEP; }

cmd_status() {
    local status reached
    status=$(state_get STATUS)
    reached=$(reached_step)
    printf 'running release:   %s\n' "$(env_value APP_VERSION)"
    printf 'last in the log:   %s (%s)\n' "$(last_deployed 2)" "$(last_deployed 6)"
    printf 'maintenance gate:  %s\n' "$([ -e "$FLAG_MAINTENANCE" ] && echo on || echo off)"
    printf 'worker gate:       %s\n' "$([ -e "$FLAG_WORKER" ] && echo on || echo off)"
    printf 'last deploy:       %s, target %s, stopped at step %s\n' \
        "${status:-none}" "$(state_get TARGET_VERSION)" "${reached:-0}"
    printf 'pre-deploy dump:   %s\n' "$(state_get DUMP)"
    printf '\nDecision tree:\n'
    if [ "$status" = failed ] && [ "${reached:-0}" -lt 4 ]; then
        printf '  -> it stopped before the gates: nothing changed, nothing to recover\n'
    elif [ "$status" = failed ] && [ "${reached:-0}" -lt 7 ]; then
        printf '  -> failed before step 7: ./recover.sh rollback (no data loss)\n'
    elif [ "$status" = failed ] && [ "${reached:-0}" -lt 10 ]; then
        printf '  -> failed at steps 7-9 (irreversible flag: %s):\n' "$(state_get IRREVERSIBLE)"
        printf '     ./recover.sh migrate-back, or ./recover.sh restore-predeploy --operator EMAIL (no data loss)\n'
    else
        printf '  -> a defect after the commit point: forward-fix first (./deploy.sh X.Y.Z+1);\n'
        printf '     ./recover.sh rollback-images only if the notes say "%s"\n' "$MARK_OLD_CODE_WORKS"
        printf '  -> only a restore can fix it, after writes were accepted: an explicit decision:\n'
        printf '     ./recover.sh restore DUMP --operator EMAIL --incident TEXT\n'
        printf '  -> the VPS is gone: on the new one, ./recover.sh restore-offsite ...\n'
    fi
}

cmd_rollback() {
    take_lock
    require_unfinished_deploy
    [ "$(reached_step)" -lt 7 ] || die "the migration may have run: use migrate-back or restore-predeploy"
    previous_or_die
    begin
    finish "$PREVIOUS_VERSION" "$PREVIOUS_APP" "$PREVIOUS_BACKUP" "$(state_get DUMP)" rollback
}

# With the new image: it knows how to go back. Only apps whose head moved are touched; an app
# the release introduced goes to zero.
migrate_back() {
    export APP_DIGEST BACKUP_DIGEST
    APP_DIGEST=$(state_get TARGET_APP_DIGEST)
    BACKUP_DIGEST=$(state_get TARGET_BACKUP_DIGEST)
    # An earlier release ran here, so its head has lines. Without them "zero" would be wrong.
    [ -s deploy.head ] || die "deploy.head is empty: the previous head is unknown"
    local head line app name saved
    head=$(migration_head) || return 1
    mapfile -t lines <<<"$head"
    for line in "${lines[@]}"; do
        read -r app name <<<"$line"
        [ -n "$app" ] || continue
        saved=$(sed -n "s/^$app //p" deploy.head)
        [ "$saved" != "$name" ] || continue
        say "migrating $app back to ${saved:-zero}"
        compose run --rm --no-deps -T migrate manage migrate "$app" "${saved:-zero}" --noinput \
            </dev/null || return 1
    done
}

cmd_migrate_back() {
    take_lock
    require_unfinished_deploy
    [ "$(state_get IRREVERSIBLE)" = 0 ] || die "the notes flag an irreversible migration: restore-predeploy"
    previous_or_die
    begin
    migrate_back || die "the migration cannot go back: ./recover.sh restore-predeploy --operator EMAIL"
    finish "$PREVIOUS_VERSION" "$PREVIOUS_APP" "$PREVIOUS_BACKUP" "$(state_get DUMP)" migrate-back
}

# Before any restore (plan §9): a pg_dump of the current database, what a reconciliation needs
# from it, and a renamed copy of the pg_data volume. PRESERVED names the directory; a database
# that cannot be read still gets its volume copied (--db-unreadable).
preserve_failed_state() { # preserve_failed_state SINCE
    local stamp since="$1"
    stamp=$(date -u +%Y%m%dT%H%M%SZ)
    PRESERVED="preserved/$stamp"
    mkdir -p "$PRESERVED"
    if [ "$DB_UNREADABLE" = 0 ]; then
        say "preserving the failed database in $PRESERVED"
        compose exec -T db sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" exec pg_dump -Fc -h 127.0.0.1 \
            -U "$POSTGRES_USER" "$POSTGRES_DB"' >"$PRESERVED/failed.dump"
        db_sql -t -A <ops/failed-state.sql >"$PRESERVED/failed-state.json"
        db_sql -t -A -v "since=$since" <ops/documents-since.sql >"$PRESERVED/written-since-deploy.json"
    fi
    local volume copy db_image
    volume="$(project_name)_pg_data"
    copy="${volume}_failed_$stamp"
    db_image=$(docker inspect --format '{{.Config.Image}}' "$(compose ps -aq db)")
    compose stop db
    docker volume create "$copy" >/dev/null
    docker run --rm --network none --entrypoint sh -v "$volume:/from:ro" -v "$copy:/to" \
        "$db_image" -c 'cp -a /from/. /to/'
    compose up -d --no-deps --wait db
    say "the failed pg_data is kept in volume $copy"
}

cmd_restore_predeploy() {
    require_operator
    take_lock
    require_unfinished_deploy
    previous_or_die
    local dump
    dump=$(state_get ORIGINAL_DUMP)
    dump="${dump:-$(state_get DUMP)}"
    [ -n "$dump" ] && [ -f "backups/$dump.dump" ] || die "the pre-deploy dump ${dump:-?} is not in backups/"
    confirm_typed RESTORE "$CONFIRM" \
        "Restoring $dump, the database as it was before this deploy. Nothing was written since."
    begin
    # The previous image reads this dump; its own commands reconcile it.
    export APP_VERSION="$PREVIOUS_VERSION" APP_DIGEST="$PREVIOUS_APP" BACKUP_DIGEST="$PREVIOUS_BACKUP"
    verify_dump "$dump"
    preserve_failed_state "$(state_get STARTED_AT)"
    restore_database "backups/$dump.dump"
    restore_media "$dump"
    rotate_secret_key
    reconcile_from_state "$OPERATOR" "$PRESERVED/failed-state.json" "$PRESERVED/reconcile-report.json"
    finish "$PREVIOUS_VERSION" "$PREVIOUS_APP" "$PREVIOUS_BACKUP" "$dump" restore-predeploy
}

cmd_rollback_images() {
    take_lock
    local version app backup notes
    version=$(last_deployed 2 2)
    app=$(last_deployed 3 2)
    backup=$(last_deployed 4 2)
    [ "$(wc -l <"$DEPLOYED_LOG")" -ge 2 ] && [ -n "$app" ] || die "no earlier release in deployed.log"
    notes=".release/$(last_deployed 2)/notes.md"
    [ -f "$notes" ] && python3 ops/checks.py flagged "$notes" "$MARK_OLD_CODE_WORKS" \
        || die "the notes of $(last_deployed 2) do not say \"$MARK_OLD_CODE_WORKS\": forward-fix or restore"
    begin
    finish "$version" "$app" "$backup" "$(last_deployed 5)" rollback-images
}

# The digests that read this dump: from deployed.log if the version ran here, else verified anew.
digests_for() { # digests_for VERSION: sets TARGET_APP and TARGET_BACKUP
    local row
    row=$(awk -F '\t' -v version="$1" '$2 == version { last = $0 } END { print last }' "$DEPLOYED_LOG" 2>/dev/null || true)
    if [ -n "$row" ]; then
        TARGET_APP=$(cut -f 3 <<<"$row")
        TARGET_BACKUP=$(cut -f 4 <<<"$row")
        return 0
    fi
    verify_release "$1" ".release/$1"
    TARGET_APP="$RELEASE_APP_DIGEST"
    TARGET_BACKUP="$RELEASE_BACKUP_DIGEST"
}

note_incident() { # note_incident ACTION DUMP SINCE
    printf '%s\t%s\tdump=%s\tloss-window=%s..now\toperator=%s\t%s\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" "$2" "${3:-unknown}" "$OPERATOR" "$INCIDENT" \
        >>"$INCIDENT_LOG"
}

cmd_restore() {
    require_operator
    [ -n "$INCIDENT" ] || die "--incident TEXT is required: this decision goes in the incident log"
    [ -n "$DUMP_ARG" ] || usage
    local dump="${DUMP_ARG%.dump}" since version
    dump="${dump##*/}"
    [ -f "backups/$dump.dump" ] && [ -f "backups/$dump.manifest.json" ] \
        || die "backups/$dump.dump and its manifest are needed"
    version="${RESTORE_VERSION:-$(python3 ops/checks.py dump-version "backups/$dump.manifest.json")}"
    [ -n "$version" ] && [ "$version" != unknown ] || die "the dump names no version: pass --version"
    take_lock
    since=$(last_deployed 1)
    digests_for "$version"
    begin
    export APP_VERSION="$version" APP_DIGEST="$TARGET_APP" BACKUP_DIGEST="$TARGET_BACKUP"
    verify_dump "$dump"
    preserve_failed_state "${since:-1970-01-01T00:00:00Z}"
    say "LOSS WINDOW: everything written from ${since:-the dump} until now, unless re-entered."
    say "What was written since the deploy, for re-entry: $PRESERVED/written-since-deploy.json"
    confirm_typed RESTORE "$CONFIRM" "Restore $dump over the current database?"
    note_incident restore "$dump" "$since"
    restore_database "backups/$dump.dump"
    restore_media "$dump"
    rotate_secret_key
    if [ "$DB_UNREADABLE" = 1 ]; then
        reconcile_without_state "$OPERATOR" "$PRESERVED/reconcile-report.json"
    else
        reconcile_from_state "$OPERATOR" "$PRESERVED/failed-state.json" "$PRESERVED/reconcile-report.json"
    fi
    finish "$version" "$TARGET_APP" "$TARGET_BACKUP" "$dump" restore
}

# The snapshot's files arrive under backups/.offsite/backups/ with restic's absolute paths.
fetch_snapshot() { # fetch_snapshot ID: sets FETCHED to the dump's base name
    compose run --rm --no-deps -T --entrypoint sh backup -c \
        'rm -rf /backups/.offsite && exec restic restore "$0" --target /backups/.offsite' "$1" \
        </dev/null
    local found
    found=$(find backups/.offsite/backups -maxdepth 1 -name 'gmat-*.dump' -print -quit)
    [ -n "$found" ] || die "snapshot $1 holds no dump"
    FETCHED=$(basename "$found" .dump)
    mv backups/.offsite/backups/"$FETCHED"* backups/
    rm -rf backups/.offsite
}

cmd_restore_offsite() {
    require_operator
    [ -n "$INCIDENT" ] || die "--incident TEXT is required"
    [ -n "$SNAPSHOT" ] && [ -n "$RESTORE_VERSION" ] || usage
    take_lock
    digests_for "$RESTORE_VERSION"
    export APP_VERSION="$RESTORE_VERSION" APP_DIGEST="$TARGET_APP" BACKUP_DIGEST="$TARGET_BACKUP"
    confirm_typed RESTORE "$CONFIRM" \
        "Restore snapshot $SNAPSHOT here. No failed state: every privileged account comes up suspended."
    begin
    fetch_snapshot "$SNAPSHOT"
    verify_dump "$FETCHED"
    note_incident restore-offsite "$FETCHED" ""
    restore_database "backups/$FETCHED.dump"
    restore_media "$FETCHED"
    rotate_secret_key
    PRESERVED="preserved/$(date -u +%Y%m%dT%H%M%SZ)"
    mkdir -p "$PRESERVED"
    reconcile_without_state "$OPERATOR" "$PRESERVED/reconcile-report.json" "${GAP_ARGS[@]+"${GAP_ARGS[@]}"}"
    finish "$RESTORE_VERSION" "$TARGET_APP" "$TARGET_BACKUP" "$FETCHED" restore-offsite
}

command="${1:-status}"
[ "$#" -eq 0 ] || shift
parse_options "$@"
case "$command" in
    status) cmd_status ;;
    rollback) cmd_rollback ;;
    migrate-back) cmd_migrate_back ;;
    restore-predeploy) cmd_restore_predeploy ;;
    rollback-images) cmd_rollback_images ;;
    restore) cmd_restore ;;
    restore-offsite) cmd_restore_offsite ;;
    -h | --help) usage ;;
    *) usage ;;
esac
