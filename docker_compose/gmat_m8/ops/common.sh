#!/usr/bin/env bash
# Shared by deploy.sh, recover.sh and restore-drill.sh (plan §9). Sourced, never executed.
#
# Settings a rehearsal or another installation can change, from the environment:
#   DEPLOY_COMPOSE_FILES       colon-separated compose files (the production overlay by default)
#   DEPLOY_LOCK_FILE           the lock deploys, recoveries and drills share
#   DEPLOY_GITHUB_REPOSITORY   owner/name of the repository that signs releases
#   DEPLOY_IMAGE_PREFIX        where the release images must live
#   DEPLOY_PUBLIC_PORT         the HTTPS port Caddy publishes
#   GITHUB_API_URL             where release files are fetched (their signatures decide anyway)
#
# Nothing here skips a check: `cosign` and `docker` are whatever PATH gives, and the identity
# they verify is built from the repository name.
# shellcheck shell=bash
# shellcheck disable=SC2034  # the scripts that source this file use these variables
# shellcheck disable=SC2016  # the single-quoted commands expand inside the db container

: "${DEPLOY_COMPOSE_FILES:=docker-compose.yml:docker-compose.production.yml}"
: "${DEPLOY_LOCK_FILE:=/var/lock/gmat-m8-deploy}"
: "${DEPLOY_GITHUB_REPOSITORY:=mano8/gmat-m8}"
: "${DEPLOY_IMAGE_PREFIX:=ghcr.io/mano8}"
: "${DEPLOY_PUBLIC_PORT:=443}"
: "${GITHUB_API_URL:=https://api.github.com}"

ENTRYPOINT=/app/docker/app/entrypoint.sh
FLAG_MAINTENANCE=flags/maintenance.on
FLAG_WORKER=flags/worker.paused
DEPLOYED_LOG=deployed.log
STATE_FILE=deploy.state
INCIDENT_LOG=incident.log
OIDC_ISSUER=https://token.actions.githubusercontent.com
RUNNING_STATES=running,outbound-off
ROLES_SCRIPT=/docker-entrypoint-initdb.d/10-roles.sh
# Markers a release's notes carry on a line of their own (docs/release.md).
MARK_IRREVERSIBLE="Irreversible migration"
MARK_OLD_CODE_WORKS="Old code works on the new schema"

say() { printf '%s %s\n' "$(date -u +%H:%M:%SZ)" "$*" >&2; }
die() {
    say "ERROR: $*"
    exit 1
}

# --- env files ------------------------------------------------------------------------------

env_value() { # env_value KEY [FILE]: the value, unquoted; never printed by the scripts
    [ -f "${2:-.env}" ] || return 0
    sed -n "s/^$1=//p" "${2:-.env}" | tail -n 1
}

set_env_value() { # set_env_value FILE KEY VALUE: replaces the line in one atomic rename
    local file="$1" key="$2" value="$3" temporary
    temporary=$(mktemp "$file.XXXXXX")
    if [ -f "$file" ]; then
        grep -v "^$key=" "$file" >"$temporary" || true
    fi
    printf '%s=%s\n' "$key" "$value" >>"$temporary"
    chmod 0600 "$temporary"
    mv "$temporary" "$file"
}

# --- compose -------------------------------------------------------------------------------

COMPOSE_ARGS=()
IFS=: read -r -a _compose_files <<<"$DEPLOY_COMPOSE_FILES"
for _file in "${_compose_files[@]}"; do
    COMPOSE_ARGS+=(-f "$_file")
done
unset _compose_files _file

compose() { docker compose "${COMPOSE_ARGS[@]}" "$@"; }

project_name() { printf '%s' "${COMPOSE_PROJECT_NAME:-gmat-m8}"; }

service_running() { [ -n "$(compose ps --status running -q "$1" 2>/dev/null)" ]; }

# --- the lock --------------------------------------------------------------------------------

take_lock() {
    exec 9>"$DEPLOY_LOCK_FILE" || die "cannot open the lock $DEPLOY_LOCK_FILE"
    if ! flock -n 9; then
        say "another deploy, recovery or drill holds the lock: waiting for it"
        flock 9
        say "lock acquired"
    fi
}

# --- the gates --------------------------------------------------------------------------------

gates_on() {
    mkdir -p flags
    : >"$FLAG_WORKER"
    : >"$FLAG_MAINTENANCE"
    chmod 0644 "$FLAG_WORKER" "$FLAG_MAINTENANCE"
}

public_status() { # public_status PATH: the status Caddy answers, from outside the containers
    local domain insecure=()
    domain=$(env_value APP_DOMAIN)
    # Caddy's internal CA on a local stack; a real domain has a real certificate to check.
    [ "$domain" != localhost ] || insecure=(--insecure)
    curl --silent --output /dev/null --write-out '%{http_code}' --max-time 10 \
        --resolve "$domain:$DEPLOY_PUBLIC_PORT:127.0.0.1" "${insecure[@]}" \
        "https://$domain:$DEPLOY_PUBLIC_PORT$1" || true
}

wait_public_status() { # wait_public_status STATUS PATH SECONDS
    local deadline=$((SECONDS + $3)) status=""
    while [ "$SECONDS" -lt "$deadline" ]; do
        status=$(public_status "$2")
        [ "$status" != "$1" ] || return 0
        sleep 2
    done
    say "$2 answers ${status:-nothing}, expected $1"
    return 1
}

worker_in_state() { # worker_in_state STATES
    compose exec -T -e "WORKER_EXPECT_STATE=$1" worker "$ENTRYPOINT" worker-healthcheck
}

wait_worker_state() { # wait_worker_state STATES SECONDS
    local deadline=$((SECONDS + $2))
    while [ "$SECONDS" -lt "$deadline" ]; do
        ! worker_in_state "$1" 2>/dev/null || return 0
        sleep 3
    done
    say "the worker's heartbeat is not in state $1"
    return 1
}

stop_writers() { compose stop web worker; }

start_services() {
    compose up -d --no-deps --no-build backup
    compose up -d --no-deps --no-build --wait --wait-timeout 300 web worker
    service_running backup || die "the backup service did not stay up"
}

# In a one-off container on `edge`, against web:8000, bypassing the gate; then the worker's
# heartbeat, which lives in the worker's own /tmp.
verify_internal() {
    compose run --rm --no-deps -T web verify-internal
    wait_worker_state paused 90
}

# Traffic and processing open together, only after verification. If the worker does not
# resume, or the public site does not answer once opened, both gates close again.
commit_point() {
    rm -f "$FLAG_WORKER"
    if ! wait_worker_state "$RUNNING_STATES" 90; then
        gates_on
        die "the worker did not resume: both gates are on again"
    fi
    rm -f "$FLAG_MAINTENANCE"
    if ! wait_public_status 200 /healthz/ 60 || ! wait_public_status 200 /accounts/login/ 30; then
        gates_on
        die "the public site does not answer: both gates are on again"
    fi
    say "commit point passed: traffic and processing are open"
}

# --- state of the deploy in progress ----------------------------------------------------------

state_set() { set_env_value "$STATE_FILE" "$1" "$2"; }
state_get() { env_value "$1" "$STATE_FILE"; }

last_deployed() { # last_deployed FIELD [ROWS_BACK]: fields: 1 time 2 version 3 app 4 backup 5 dump
    [ -f "$DEPLOYED_LOG" ] || return 0
    tail -n "${2:-1}" "$DEPLOYED_LOG" | head -n 1 | cut -f "$1"
}

record_deployed() { # record_deployed VERSION APP BACKUP DUMP KIND
    printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" "$2" "$3" "$4" "$5" \
        >>"$DEPLOYED_LOG"
}

use_digests() { # use_digests VERSION APP BACKUP: this process and .env from now on
    export APP_VERSION="$1" APP_DIGEST="$2" BACKUP_DIGEST="$3"
    set_env_value .env APP_VERSION "$1"
    set_env_value .env APP_DIGEST "$2"
    set_env_value .env BACKUP_DIGEST "$3"
}

# --- verifying a release ------------------------------------------------------------------------

github_get() { # github_get URL OUTPUT [ACCEPT]: the token goes through stdin, never argv
    local token
    token=$(env_value GITHUB_READ_TOKEN)
    {
        [ -z "$token" ] || printf 'header = "Authorization: Bearer %s"\n' "$token"
    } | curl --config - --fail --silent --show-error --location --max-time 60 --retry 2 \
        --header "Accept: ${3:-application/vnd.github+json}" --output "$2" "$1"
}

registry_login() {
    local token
    token=$(env_value GITHUB_READ_TOKEN)
    case "$DEPLOY_IMAGE_PREFIX" in
        ghcr.io/*)
            [ -z "$token" ] || printf '%s' "$token" | docker login ghcr.io \
                --username "${DEPLOY_GITHUB_REPOSITORY%%/*}" --password-stdin >/dev/null
            REGISTRY_LOGGED_IN=1
            ;;
    esac
}

registry_logout() {
    [ "${REGISTRY_LOGGED_IN:-0}" != 1 ] || docker logout ghcr.io >/dev/null 2>&1 || true
}

signature_identity() { # signature_identity VERSION
    printf 'https://github.com/%s/.github/workflows/docker-publish.yaml@refs/tags/v%s' \
        "$DEPLOY_GITHUB_REPOSITORY" "$1"
}

# Downloads, verifies and pulls a release; RELEASE_* hold what it vouches for and $2 keeps its
# files (notes.md among them). Anything off stops here, before the first gate.
verify_release() { # verify_release VERSION DIRECTORY
    local version="$1" directory="$2" identity manifest bundle url
    mkdir -p "$directory"
    manifest="$directory/release-manifest.json"
    bundle="$manifest.sigstore.json"
    identity=$(signature_identity "$version")
    say "fetching release v$version of $DEPLOY_GITHUB_REPOSITORY"
    github_get "$GITHUB_API_URL/repos/$DEPLOY_GITHUB_REPOSITORY/releases/tags/v$version" \
        "$directory/release.json" || die "the release v$version could not be fetched"
    for name in release-manifest.json release-manifest.json.sigstore.json; do
        url=$(python3 ops/checks.py asset "$directory/release.json" "$name") \
            || die "release v$version has no $name"
        github_get "$url" "$directory/$name" application/octet-stream \
            || die "$name could not be downloaded"
    done
    python3 ops/checks.py notes "$directory/release.json" >"$directory/notes.md"

    say "verifying the manifest's signature"
    cosign verify-blob --bundle "$bundle" --certificate-identity "$identity" \
        --certificate-oidc-issuer "$OIDC_ISSUER" "$manifest" >/dev/null 2>&1 \
        || die "the signature of release-manifest.json does not verify: refused"

    local values
    values=$(python3 ops/checks.py manifest "$manifest" --version "$version" \
        --repository "$DEPLOY_GITHUB_REPOSITORY" --image-prefix "$DEPLOY_IMAGE_PREFIX") \
        || die "release-manifest.json does not match this release: refused"
    while IFS='=' read -r key value; do
        case "$key" in
            RELEASE_*) printf -v "$key" '%s' "$value" ;;
        esac
    done <<<"$values"

    registry_login
    local ref
    for ref in "$RELEASE_APP_REF" "$RELEASE_BACKUP_REF"; do
        say "verifying and pulling $ref"
        cosign verify --certificate-identity "$identity" \
            --certificate-oidc-issuer "$OIDC_ISSUER" "$ref" >/dev/null 2>&1 \
            || die "the signature of $ref does not verify: refused"
        docker pull --quiet "$ref" >/dev/null || die "$ref could not be pulled"
        docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$ref" \
            | grep -qxF "$ref" || die "$ref is not what the registry delivered: refused"
    done
}

# --- the database ---------------------------------------------------------------------------------

db_sql() { # db_sql [psql args...]: SQL on stdin, as the bootstrap superuser, on the app database
    compose exec -T db sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" exec psql -X -q -v ON_ERROR_STOP=1 \
        -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" "$@"' sh "$@"
}

migration_head() { # one line "app migration" per app; nothing before the first migration
    local exists
    exists=$(printf "SELECT to_regclass('public.django_migrations') IS NOT NULL;\n" \
        | db_sql -t -A) || return 1
    [ "$exists" = t ] || return 0
    db_sql -t -A -F ' ' <<'SQL'
SELECT DISTINCT ON (app) app, name FROM django_migrations ORDER BY app, id DESC;
SQL
}

restore_roles() {
    compose exec -T db sh -c "PGPASSWORD=\"\$POSTGRES_PASSWORD\" exec sh $ROLES_SCRIPT"
}

# Replaces the application database with a dump: a new database, the role bootstrap (grants on
# the database and schema), the restore as the owner role with no owners or privileges from the
# dump, the bootstrap again (grants on what was restored). Any error stops the restore.
restore_database() { # restore_database DUMP_FILE
    compose exec -T db sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" exec psql -X -q -v ON_ERROR_STOP=1 \
        -h 127.0.0.1 -U "$POSTGRES_USER" -d postgres -v db="$POSTGRES_DB"' <<'SQL'
DROP DATABASE IF EXISTS :"db" WITH (FORCE);
CREATE DATABASE :"db" TEMPLATE template0 ENCODING 'UTF8';
SQL
    restore_roles
    compose exec -T db sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" exec pg_restore -h 127.0.0.1 \
        -U "$POSTGRES_USER" -d "$POSTGRES_DB" --role="$GMAT_DB_OWNER_USER" \
        --no-owner --no-privileges --exit-on-error' <"$1"
    restore_roles
    printf 'ANALYZE;\n' | db_sql
}

# The attachments a dump's snapshot listed, copied next to it by backup.sh. A file already on
# the volume is the same file: its name is its key, and stored files never change.
restore_media() { # restore_media DUMP_BASE
    [ -d "backups/$1.media" ] || return 0
    compose run --rm --no-deps -T -v "$PWD/backups/$1.media:/restored-media:ro" \
        --entrypoint sh web -c 'cp -np /restored-media/. /srv/media/files/'
}

verify_dump() { # verify_dump DUMP_BASE: checksum, restore into a scratch cluster, manifest
    compose run --rm --no-deps -T --entrypoint restore backup --verify "$1.dump"
}

# --- reconciliation ----------------------------------------------------------------------------------

# Plan §9, step 1: a new SECRET_KEY, no fallback. Sessions, reset links and signed form keys of
# before the restore stop verifying. The MFA and outbox keys stay: enrollments survive.
rotate_secret_key() {
    local value
    while :; do
        value=$(head -c 48 /dev/urandom | base64 | tr -d '\n=' | tr '+/' '-_')
        printf '%s' "$value" | tr '[:upper:]' '[:lower:]' | grep -qE \
            'change|placeholder|example|password|secret|replace|xxxx|dev-|test-|build-|insecure|^your-' \
            || break
    done
    set_env_value app.env DJANGO_SECRET_KEY "$value"
    say "DJANGO_SECRET_KEY rotated"
}

reconcile_from_state() { # reconcile_from_state OPERATOR STATE_FILE REPORT
    compose run --rm --no-deps -T web post-restore-reconcile --operator "$1" \
        --failed-state - <"$2" | tee "$3"
}

reconcile_without_state() { # reconcile_without_state OPERATOR REPORT [--gap-... EMAIL ...]
    local operator="$1" report="$2"
    shift 2
    compose run --rm --no-deps -T web post-restore-reconcile --operator "$operator" \
        --no-failed-state "$@" </dev/null | tee "$report"
}

confirm_typed() { # confirm_typed WORD GIVEN PROMPT: GIVEN is the --confirm option's value
    local word="$1" given="$2" prompt="$3" answer=""
    if [ -n "$given" ]; then
        [ "$given" = "$word" ] || die "--confirm does not match: expected $word"
        return 0
    fi
    [ -t 0 ] || die "$prompt (not a terminal: pass --confirm $word)"
    printf '%s\nType %s to continue: ' "$prompt" "$word" >&2
    read -r answer
    [ "$answer" = "$word" ] || die "not confirmed"
}
