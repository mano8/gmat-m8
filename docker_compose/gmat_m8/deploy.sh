#!/usr/bin/env bash
# Production deploy (plan §9, F18, F23, F43): ./deploy.sh X.Y.Z, on the VPS, by the maintainer.
#
#   ./deploy.sh X.Y.Z [--confirm X.Y.Z] [--resume]
#
#   --confirm   non-interactive answer to the prompt of a release flagged irreversible
#   --resume    go on although an earlier deploy or recovery left the gates on (a fix-forward
#               release after a failure). Both gates stay on until this one's commit point.
#
#   1  lock                the same flock as recover.sh and the restore drill
#   2  verify the release  signed manifest, signed images, both pulled by digest; any mismatch aborts
#   3  preflight           with the new image, read-only: migrate-plan, check --deploy, the offsite
#                          repository answers; a flagged irreversible migration needs typing the version
#   4  gates on            flags/maintenance.on + flags/worker.paused; 503 confirmed from outside
#   5  stop the writers    web and worker; what the release changed in db or Caddy applies now
#   6  pre-deploy backup   dump + manifest + offsite copy; the dump id is recorded
#   7  migrate             with the new image, as the owner role
#   8  start               web, worker, backup on the new digests; traffic gated, worker paused
#   9  verify internally   verify-internal against web:8000, the paused worker's heartbeat
#   10 commit point        worker.paused off, then maintenance.on off; checked from outside
#   11 record              version, both digests, dump id, time in deployed.log
#
# Any failure before step 10 stops with both gates still on: no public write, no email. The
# script prints the matching row of the decision tree; the human decides, recover.sh executes.
set -Eeuo pipefail
umask 077
cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
# shellcheck source=ops/common.sh
. ops/common.sh

usage() {
    sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//' >&2
    exit 64
}

VERSION=""
CONFIRM=""
RESUME=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --confirm)
            [ "$#" -ge 2 ] || usage
            CONFIRM="$2"
            shift 2
            ;;
        --resume)
            RESUME=1
            shift
            ;;
        -h | --help) usage ;;
        -*) usage ;;
        *)
            [ -z "$VERSION" ] || usage
            VERSION="$1"
            shift
            ;;
    esac
done
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z][0-9A-Za-z.-]*)?$ ]] || usage

step() { # step N TEXT
    state_set STEP "$1"
    say "step $1: $2"
}

decision_row() {
    local reached
    reached=$(state_get STEP)
    if [ "${reached:-0}" -lt 4 ]; then
        say "Stopped at step ${reached:-0}, before the gates: nothing changed and the site stayed open."
        return 0
    fi
    say "Stopped at step $reached with both gates on: no public write and no email since step 5."
    say "./recover.sh status tells the rest. The matching row of the decision tree:"
    if [ "$reached" -lt 7 ]; then
        say "  failed before step 7:  ./recover.sh rollback (previous digests; no data loss)"
    elif [ "$reached" -lt 10 ]; then
        say "  failed at steps 7-9:   ./recover.sh migrate-back if the release is reversible,"
        say "                         else ./recover.sh restore-predeploy --operator EMAIL (no data loss)"
    else
        say "  at the commit point:   forward-fix first (./deploy.sh X.Y.Z+1 --resume);"
        say "                         ./recover.sh status lists the alternatives"
    fi
}

on_exit() {
    local status=$?
    registry_logout
    if [ "$status" -ne 0 ] && [ -f "$STATE_FILE" ] && [ "$(state_get STATUS)" = running ]; then
        state_set STATUS failed
        decision_row
    fi
    exit "$status"
}

refuse_unfinished() {
    [ "$RESUME" = 0 ] || return 0
    local reached
    reached=$(state_get STEP)
    if [ -e "$FLAG_MAINTENANCE" ] || [ -e "$FLAG_WORKER" ] \
        || { [ "$(state_get STATUS)" = failed ] && [ "${reached:-0}" -ge 4 ]; }; then
        die "an earlier deploy or recovery did not finish (see ./recover.sh status); decide there, or pass --resume"
    fi
}

require_tools() {
    local tool
    for tool in docker curl flock timeout cosign python3 sha256sum; do
        command -v "$tool" >/dev/null || die "$tool is not installed"
    done
    docker compose version >/dev/null || die "the docker compose plugin is not installed"
}

check_env_files() {
    local file
    for file in .env app.env; do
        [ -f "$file" ] || die "$file is missing (app.env.production.example, plan H8.6)"
        [ "$(stat -c %a "$file")" = 600 ] || die "$file must be mode 0600"
    done
    if [ -z "$(env_value RESTIC_REPOSITORY)" ] && [ "${DEPLOY_ALLOW_NO_OFFSITE:-0}" != 1 ]; then
        die "RESTIC_REPOSITORY is empty: the pre-deploy dump needs its offsite copy"
    fi
    if [ "${DEPLOY_ALLOW_OPEN_ADMIN:-0}" != 1 ]; then
        python3 ops/checks.py admin-cidrs "$(env_value ADMIN_ALLOWED_CIDRS)" \
            || die "set ADMIN_ALLOWED_CIDRS (exception X10 in docs/security.md)"
    fi
}

# The compose files on this machine must be the release's own. Outside a git checkout (a
# rehearsal, an unpacked archive) there is nothing to compare.
check_checkout() {
    local head
    head=$(git rev-parse HEAD 2>/dev/null) || {
        say "not a git checkout: the compose files' version cannot be compared with the release"
        return 0
    }
    [ "$head" = "$RELEASE_COMMIT" ] || die \
        "this checkout is $head, the release is $RELEASE_COMMIT: git fetch --tags && git switch --detach v$VERSION"
}

# A release that follows a failed one (--resume) goes back to where the failed one started:
# the head saved before that migration, and the dump taken before it.
begin_state() {
    local reached original
    reached=$(state_get STEP)
    original=""
    if [ "$RESUME" = 1 ] && [ "${reached:-0}" -ge 7 ]; then
        original=$(state_get ORIGINAL_DUMP)
        original="${original:-$(state_get DUMP)}"
    fi
    rm -f "$STATE_FILE"
    [ -z "$original" ] || state_set ORIGINAL_DUMP "$original"
    state_set TARGET_VERSION "$VERSION"
    state_set TARGET_APP_DIGEST "$RELEASE_APP_DIGEST"
    state_set TARGET_BACKUP_DIGEST "$RELEASE_BACKUP_DIGEST"
    state_set PREVIOUS_VERSION "$(last_deployed 2)"
    state_set PREVIOUS_APP_DIGEST "$(last_deployed 3)"
    state_set PREVIOUS_BACKUP_DIGEST "$(last_deployed 4)"
    state_set STARTED_AT "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    state_set STEP 2
    state_set STATUS running
}

# Before the gates: restic retries a repository that does not answer for about 15 minutes, which
# step 6 would spend with the site closed. A first deploy's backup creates the repository.
check_offsite() {
    [ -n "$(env_value RESTIC_REPOSITORY)" ] || return 0
    [ -n "$(last_deployed 2)" ] || return 0
    timeout 180 docker compose "${COMPOSE_ARGS[@]}" run --rm --no-deps -T --entrypoint restic \
        backup cat config </dev/null >/dev/null \
        || die "the offsite repository does not answer: nothing changed, the site stays open"
}

# This process, not .env, carries the new digests until step 8: .env says what is running.
preflight() {
    export APP_DIGEST="$RELEASE_APP_DIGEST" BACKUP_DIGEST="$RELEASE_BACKUP_DIGEST"
    local images
    images=$(compose config --images)
    grep -qxF "$RELEASE_APP_REF" <<<"$images" \
        || die "the compose files do not run $RELEASE_APP_REF: refused"
    grep -qxF "$RELEASE_BACKUP_REF" <<<"$images" \
        || die "the compose files do not run $RELEASE_BACKUP_REF: refused"
    service_running db || compose up -d --no-deps --wait db
    compose run --rm --no-deps -T web migrate-plan </dev/null
    compose run --rm --no-deps -T web check </dev/null
    check_offsite
    state_set IRREVERSIBLE 0
    if python3 ops/checks.py flagged ".release/$VERSION/notes.md" "$MARK_IRREVERSIBLE"; then
        state_set IRREVERSIBLE 1
        confirm_typed "$VERSION" "$CONFIRM" \
            "The notes of $VERSION flag an irreversible migration: going back means restoring the dump."
    fi
}

backup_before_migrating() {
    local output dump
    output=$(compose run --rm --no-deps -T backup run --wait 1800 \
        --label "pre-deploy-$VERSION" </dev/null) || die "the pre-deploy backup failed"
    dump=$(printf '%s\n' "$output" | tail -n 1)
    [[ "$dump" == gmat-* ]] || die "the backup named no dump"
    state_set DUMP "$dump"
    say "pre-deploy dump: $dump"
}

main() {
    trap on_exit EXIT
    take_lock
    refuse_unfinished
    require_tools
    check_env_files

    say "step 2: verifying release $VERSION"
    verify_release "$VERSION" ".release/$VERSION"
    check_checkout
    begin_state

    step 3 "preflight with the new image (read-only)"
    preflight

    step 4 "gates on"
    gates_on
    compose up -d --no-deps caddy
    wait_public_status 503 /healthz/ 120 || die "the gate does not answer 503 from outside"

    step 5 "stopping the writers"
    stop_writers
    # What this release changed in the database or Caddy applies here, behind the gates. The
    # Caddyfile is a bind mount: only a restart reads it, and `up` does not see it changed.
    compose up -d --no-deps --wait db caddy
    compose restart caddy
    wait_public_status 503 /healthz/ 120 || die "the gate does not answer 503 after Caddy's restart"

    step 6 "pre-deploy backup"
    backup_before_migrating
    compose stop backup

    step 7 "migrate"
    # Removed at every commit point, so one that is here belongs to an unfinished migration.
    [ -s deploy.head ] || migration_head >deploy.head
    compose run --rm --no-deps -T migrate </dev/null

    step 8 "starting the new digests; traffic gated, worker paused"
    use_digests "$VERSION" "$RELEASE_APP_DIGEST" "$RELEASE_BACKUP_DIGEST"
    start_services

    step 9 "verifying internally"
    verify_internal

    step 10 "commit point"
    commit_point

    step 11 "recording"
    record_deployed "$VERSION" "$RELEASE_APP_DIGEST" "$RELEASE_BACKUP_DIGEST" \
        "$(state_get DUMP)" deploy
    state_set STATUS "done"
    state_set DEPLOYED_AT "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    rm -f deploy.head
    say "deployed $VERSION"
}

main
