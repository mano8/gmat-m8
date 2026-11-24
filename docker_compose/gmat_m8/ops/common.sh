#!/usr/bin/env bash
# Shared by deploy.sh, recover.sh and restore-drill.sh (plan §9); init.sh uses its helper for
# backups/ (ADR-0033). Sourced, never executed.
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
# shellcheck disable=SC2015  # `a && b || die` stops either way

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
# What a release must state for rollback-images (the signed manifest's migration.old_code_works).
MARK_OLD_CODE_WORKS="Old code works on the new schema"

# backups/ under either daemon (ADR-0033, docs/rootless-host.md). Constants, never read from the
# environment: the containers' uid and gid (the images' user), where a rootless daemon's process
# and this account's subordinate ids are read, and the names backup.sh and restic give.
CONTAINER_UID=1000
CONTAINER_GID=1000
PROC_ROOT=/proc
SUBUID_FILE=/etc/subuid
SUBGID_FILE=/etc/subgid
BACKUPS_DIR="$PWD/backups"
DUMP_PATTERN='^gmat-[0-9]{8}T[0-9]{6}Z(-[A-Za-z0-9._-]{1,40})?$'
SNAPSHOT_PATTERN='^[0-9a-f]{8,64}$'
# Found out, never inherited: detect_daemon and _namespace_target set them.
DAEMON_KIND=""
NAMESPACE_PID=""
ROOTLESSKIT_PID=""
# RootlessKit's API, in the state directory dockerd-rootless.sh gives it under XDG_RUNTIME_DIR.
ROOTLESSKIT_API=dockerd-rootless/api.sock

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

# flags/ is deploy's. Caddy reads it as the containers' root; the worker as uid 1000, which under
# a rootless daemon is a shifted uid that sees the flags only through the directory's "other"
# bits (ADR-0033). One it cannot search hides worker.paused from it: refused, never widened.
flags_searchable() { # a missing flags/ is created searchable by gates_on
    local mode
    [ -e flags ] || return 0
    mode=$(stat -c %a flags) || return 1
    [[ "$mode" =~ ^[0-7]+$ ]] && ((8#$mode & 1))
}

require_flags_searchable() {
    flags_searchable || die "flags/ is mode $(stat -c %a flags 2>/dev/null || echo unknown): the worker could not see worker.paused. Set it back: chmod 0755 flags"
}

# Checked before the first flag is written: a refusal leaves both gates as they were.
gates_on() {
    require_flags_searchable
    [ -d flags ] || mkdir -m 0755 flags
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

# Waits end on the clock AND after a few tries: on a host under pressure a single slow call must
# not use up the time and close the gates on a deploy that is fine.
MIN_TRIES=5

wait_public_status() { # wait_public_status STATUS PATH SECONDS
    local deadline=$((SECONDS + $3)) status="" tries=0
    while :; do
        status=$(public_status "$2")
        [ "$status" != "$1" ] || return 0
        tries=$((tries + 1))
        [ "$SECONDS" -lt "$deadline" ] || [ "$tries" -lt "$MIN_TRIES" ] || break
        sleep 2
    done
    say "$2 answers ${status:-nothing}, expected $1"
    return 1
}

worker_in_state() { # worker_in_state STATES
    timeout 60 docker compose "${COMPOSE_ARGS[@]}" exec -T -e "WORKER_EXPECT_STATE=$1" worker \
        "$ENTRYPOINT" worker-healthcheck </dev/null
}

wait_worker_state() { # wait_worker_state STATES SECONDS
    local deadline=$((SECONDS + $2)) tries=0
    until worker_in_state "$1" 2>/dev/null; do
        tries=$((tries + 1))
        if [ "$SECONDS" -ge "$deadline" ] && [ "$tries" -ge "$MIN_TRIES" ]; then
            say "the worker's heartbeat is not in state $1"
            return 1
        fi
        sleep 3
    done
}

stop_writers() { compose stop web worker; }

# The scanner is recreated every time: its configuration is a bind mount, which `up` does not see
# changed. All of this is behind the gates, and verify_internal asks both services for an answer.
start_services() {
    compose up -d --no-deps --no-build backup
    compose up -d --no-deps --no-build freshclam
    compose up -d --no-deps --no-build --force-recreate --wait --wait-timeout 300 clamd
    compose up -d --no-deps --no-build --wait --wait-timeout 120 decoder
    compose up -d --no-deps --no-build --wait --wait-timeout 300 web worker
    service_running backup || die "the backup service did not stay up"
}

# In a one-off container on `edge`, against web:8000, bypassing the gate; then the worker's
# heartbeat, which lives in the worker's own /tmp.
verify_internal() {
    compose run --rm --no-deps -T web verify-internal </dev/null
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

# How far the deploy in progress really got. A fix-forward (--resume) counts its steps from the
# start, but what the failed one did stays done: gates that are on, a migration that ran.
reached_step() {
    local reached
    reached=$(state_get STEP)
    reached="${reached:-0}"
    if [ "$(state_get MIGRATION_RAN)" = 1 ] && [ "$reached" -lt 7 ]; then
        printf '7'
    elif { [ -e "$FLAG_MAINTENANCE" ] || [ -e "$FLAG_WORKER" ]; } && [ "$reached" -lt 4 ]; then
        printf '4'
    else
        printf '%s' "$reached"
    fi
}

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
# files (the signed manifest among them). Anything off stops here, before the first gate.
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

# --- names ----------------------------------------------------------------------------------------

# A dump's or a snapshot's name reaches a path, the helper or a container only after it matched
# its pattern, in the C locale so that a range never takes in a letter of another alphabet.
dump_name_ok() {
    local LC_ALL=C
    [[ "$1" =~ $DUMP_PATTERN ]]
}

require_dump_name() { # require_dump_name NAME
    dump_name_ok "$1" \
        || die "not a dump name: $(printf '%q' "$1") (gmat-<UTC stamp>[-<label>], as backup.sh names them)"
}

require_snapshot_id() { # require_snapshot_id ID
    local LC_ALL=C
    [[ "$1" =~ $SNAPSHOT_PATTERN ]] \
        || die "not a restic snapshot id: $(printf '%q' "$1") (8 to 64 hexadecimal digits)"
}

# --- which daemon (ADR-0033) ------------------------------------------------------------------

# What the daemon the scripts are about to use says of itself, never a setting; asked once per run
# (call it in the script's own shell, not in a $(...), for the answer to stay). A root daemon that
# remaps users (userns-remap) is refused: its files under backups/ belong to a range that this
# account cannot enter.
detect_daemon() { # sets DAEMON_KIND: root or rootless
    [ -z "$DAEMON_KIND" ] || return 0
    local options
    options=$(docker info --format '{{json .SecurityOptions}}' 2>/dev/null) \
        || die "the Docker daemon does not answer (docker info): start it, or set DOCKER_HOST (docs/rootless-host.md)"
    case "$options" in
        *'"name=userns'*) die "this daemon remaps users (userns-remap), which is not supported: run it rootless (ADR-0033)" ;;
        *'"name=rootless'*) DAEMON_KIND=rootless ;;
        '['*']' | null) DAEMON_KIND=root ;;
        *) die "docker info did not list the daemon's security options" ;;
    esac
}

# --- the rootless daemon's user namespace (docs/rootless-host.md, "Entering the namespace") -----

_owner() { stat -c %u -- "$1" 2>/dev/null || true; }

_exe_name() { # _exe_name PID: the name of the file the process runs (replaced since: " (deleted)")
    local exe
    exe=$(readlink -- "$PROC_ROOT/$1/exe" 2>/dev/null) || return 0
    exe="${exe% (deleted)}"
    printf '%s' "${exe##*/}"
}

_own_process() { # _own_process PID NAME: this account's process, running a file called NAME
    [[ "$1" =~ ^[1-9][0-9]{0,9}$ ]] && [ "$(_owner "$PROC_ROOT/$1")" = "$(id -u)" ] \
        && [ "$(_exe_name "$1")" = "$2" ]
}

# One map of the daemon's namespace (uid_map or gid_map) gives 0 to this account's own id alone,
# maps the containers' id, and maps nothing that is not one of this account's subordinate ids.
_maps_only_mine() { # _maps_only_mine MAP SUBORDINATE_FILE OWN_ID CONTAINER_ID
    [ -r "$1" ] && [ -r "$2" ] || return 1
    awk -v own="$3" -v container="$4" -v user="$(id -un)" -v uid="$(id -u)" '
        FILENAME == ARGV[1] {
            split($0, entry, ":")
            if (entry[1] == user || entry[1] == uid) { start[++ranges] = entry[2] + 0; size[ranges] = entry[3] + 0 }
            next
        }
        NF != 3 { bad = 1; next }
        $1 + 0 == 0 { root = ($2 + 0 == own + 0 && $3 + 0 == 1); next }
        {
            inside = 0
            for (i = 1; i <= ranges; i++)
                if ($2 + 0 >= start[i] && $2 + $3 <= start[i] + size[i]) inside = 1
            if (!inside) bad = 1
            if (container + 0 >= $1 + 0 && container + 0 < $1 + $3) mapped = 1
        }
        END { exit !(root && mapped && !bad) }
    ' "$2" "$1"
}

# The pid checked below is the daemon the CLI talks to only if the CLI talks to the socket next
# to it: a tcp:// or ssh:// endpoint, or another account's socket, is refused.
_daemon_socket_is() { # _daemon_socket_is PATH
    local endpoint wanted found=""
    endpoint="${DOCKER_HOST:-$(docker context inspect --format '{{.Endpoints.docker.Host}}' 2>/dev/null || true)}"
    wanted=$(readlink -f -- "$1" 2>/dev/null || true)
    [[ "$endpoint" != unix://* ]] || found=$(readlink -f -- "${endpoint#unix://}" 2>/dev/null || true)
    [ -n "$wanted" ] && [ "$found" = "$wanted" ] \
        || die "the Docker CLI talks to ${endpoint:-an unknown endpoint}, not the rootless daemon's socket: export DOCKER_HOST=unix://$1"
}

# The pid whose user namespace backups_do enters, checked every time before it is entered: this
# account's dockerd (docker.pid holds dockerd's pid, not rootlesskit's), started by this account's
# rootlesskit, in a user namespace that is not this shell's and maps only this account's ids.
# Stops at the first check that fails, saying why; sets NAMESPACE_PID and ROOTLESSKIT_PID.
_namespace_target() {
    local runtime="${XDG_RUNTIME_DIR:-}" me pid="" parent="" namespace="" own=""
    me=$(id -un)
    [ -n "$runtime" ] || die "XDG_RUNTIME_DIR is not set: the rootless daemon cannot be found (log in as $me)"
    _daemon_socket_is "$runtime/docker.sock"
    [ -f "$runtime/docker.pid" ] && [ "$(_owner "$runtime/docker.pid")" = "$(id -u)" ] \
        || die "$runtime/docker.pid is missing or not $me's: is the rootless daemon running?"
    pid=$(head -c 32 -- "$runtime/docker.pid") || pid=""
    _own_process "$pid" dockerd || die "$runtime/docker.pid does not name $me's dockerd"
    parent=$(sed -n 's/^PPid:[[:space:]]*//p' -- "$PROC_ROOT/$pid/status" 2>/dev/null) || parent=""
    _own_process "$parent" rootlesskit || die "dockerd ($pid) was not started by $me's rootlesskit"
    namespace=$(readlink -- "$PROC_ROOT/$pid/ns/user" 2>/dev/null) || namespace=""
    own=$(readlink -- "$PROC_ROOT/self/ns/user" 2>/dev/null) || own=""
    [ -n "$namespace" ] && [ -n "$own" ] && [ "$namespace" != "$own" ] \
        || die "dockerd ($pid) is not in a user namespace of its own (is this shell already inside it?)"
    _maps_only_mine "$PROC_ROOT/$pid/uid_map" "$SUBUID_FILE" "$(id -u)" "$CONTAINER_UID" \
        && _maps_only_mine "$PROC_ROOT/$pid/gid_map" "$SUBGID_FILE" "$(id -g)" "$CONTAINER_GID" \
        || die "the user namespace of dockerd ($pid) maps ids that are not $me's own and subordinate ones ($SUBUID_FILE, $SUBGID_FILE)"
    command -v nsenter >/dev/null || die "nsenter is not installed (util-linux)"
    NAMESPACE_PID="$pid"
    ROOTLESSKIT_PID="$parent"
}

# --- the rootless daemon's port driver (ADR-0033 point 4, docs/rootless-host.md) ----------------

# What the running RootlessKit says of itself, then the --port-driver it was started with.
# Prints the driver (and its protocols); 0 keeps the visitor's address, 2 hides it or does not
# carry UDP (the reason on stderr), 3 neither source tells; 1 called before _namespace_target.
port_driver() {
    local info code=0 cmdline="$PROC_ROOT/$ROOTLESSKIT_PID/cmdline"
    if [ -z "$NAMESPACE_PID" ] || [ -z "$ROOTLESSKIT_PID" ]; then
        printf 'the rootless daemon was not checked first\n' >&2
        return 1
    fi
    if info=$(curl --silent --fail --max-time 5 \
        --unix-socket "$XDG_RUNTIME_DIR/$ROOTLESSKIT_API" http://rootlesskit/v1/info 2>/dev/null); then
        info=$(printf '%s' "$info" | python3 ops/checks.py port-driver --from api - 2>&1) || code=$?
        case "$code" in
            0) printf '%s\n' "$info" && return 0 ;;
            3) ;; # an answer without a driver: the command line may still tell
            *) printf '%s\n' "$info" >&2 && return "$code" ;;
        esac
    fi
    if [ ! -r "$cmdline" ]; then
        printf "neither RootlessKit's API nor its command line answers\n" >&2
        return 3
    fi
    python3 ops/checks.py port-driver --from cmdline - <"$cmdline"
}

# --- the one helper for backups/ (ADR-0033) ----------------------------------------------------

# Every host-side read or write under backups/ goes through backups_do (the list and the
# operations: docs/rootless-host.md). Each operation is a fixed command of a tool named by its
# absolute path; its only variable parts are a dump's base name that matched DUMP_PATTERN and the
# drill's directory, each passed as its own argument and never parsed by a shell again. Under a
# rootless daemon the command runs in the daemon's user namespace, where this account is root
# over the containers' files: only that namespace (-U), so paths keep the host's meaning. Under a
# root daemon it runs as it is. It never falls back to reading as this account, never widens a
# mode, and never makes a file this account's.
backups_do() { # backups_do OPERATION [ARGUMENTS...]
    local operation="${1:-}" arity
    case "$operation" in
        prepare | stat | find-fetched | drop-offsite | wipe-dumps) arity=0 ;;
        adopt) arity=1 ;;
        exists | read | import) arity=2 ;;
        *) die "backups_do: no operation $(printf '%q' "$operation")" ;;
    esac
    shift
    [ "$#" -eq "$arity" ] || die "backups_do $operation takes $arity argument(s)"
    detect_daemon
    "_backups_${operation//-/_}" "$@"
}

# Before anything changes: the daemon is known (in this shell) and backups/ can be reached.
backups_ready() {
    detect_daemon
    backups_do stat >/dev/null || die "backups/ cannot be reached: run bash init.sh (docs/rootless-host.md)"
}

_backups_run() { # TOOL ARGUMENTS...: the tool named by its absolute path
    detect_daemon
    if [ "$DAEMON_KIND" = rootless ]; then
        _namespace_target
        nsenter -U --preserve-credentials -t "$NAMESPACE_PID" -- "$@"
    else
        "$@"
    fi
}

_backups_entry() { # _backups_entry dump|manifest|media BASE: sets ENTRY_NAME, ENTRY_TYPE (find's %y)
    require_dump_name "$2"
    case "$1" in
        dump) ENTRY_NAME="$2.dump" ENTRY_TYPE=f ;;
        manifest) ENTRY_NAME="$2.manifest.json" ENTRY_TYPE=f ;;
        media) ENTRY_NAME="$2.media" ENTRY_TYPE=d ;;
        *) die "backups_do: no entry kind $(printf '%q' "$1")" ;;
    esac
}

_dump_set() { # _dump_set BASE: sets DUMP_SET, find's test for a dump, its manifest and its media
    require_dump_name "$1"
    DUMP_SET=('(' -name "$1.dump" -o -name "$1.manifest.json" -o -name "$1.media" ')')
}

# backups/ as the containers see it: theirs and 0700. Under a rootless daemon everything in it
# becomes theirs too, so the dumps of a host that ran the root daemon stay readable to them.
_backups_prepare() {
    detect_daemon
    _backups_run /usr/bin/mkdir -p -- "$BACKUPS_DIR"
    if [ "$DAEMON_KIND" = rootless ]; then
        _backups_run /usr/bin/chown -R --no-dereference "$CONTAINER_UID:$CONTAINER_GID" -- "$BACKUPS_DIR"
    fi
    _backups_run /usr/bin/chmod 0700 -- "$BACKUPS_DIR"
}

_backups_stat() { _backups_run /usr/bin/stat -c '%u %a' -- "$BACKUPS_DIR"; }

# 0 there, 1 not there. Anything else stops the script: one that cannot be read, or an entry of
# another type (a link, say), is never taken for "not there".
_backups_exists() { # exists dump|manifest|media BASE
    _backups_entry "$1" "$2"
    local found
    found=$(_backups_run /usr/bin/find "$BACKUPS_DIR" -mindepth 1 -maxdepth 1 -name "$ENTRY_NAME" \
        -printf '%y') || die "backups/ cannot be read"
    case "$found" in
        '') return 1 ;;
        "$ENTRY_TYPE") return 0 ;;
        *) die "backups/$ENTRY_NAME is not a plain $([ "$ENTRY_TYPE" = f ] && echo file || echo directory): refused" ;;
    esac
}

_backups_read() { # read dump|manifest BASE: the file on stdout
    _backups_entry "$1" "$2"
    [ "$ENTRY_TYPE" = f ] || die "backups_do read: a dump or a manifest only"
    _backups_run /usr/bin/cat -- "$BACKUPS_DIR/$ENTRY_NAME"
}

# The first dump a restic restore left in backups/.offsite/backups; a link is never followed.
_backups_find_fetched() {
    _backups_run /usr/bin/find "$BACKUPS_DIR/.offsite/backups" -mindepth 1 -maxdepth 1 -type f \
        -name 'gmat-*.dump' -print -quit
}

_backups_adopt() { # adopt BASE: the fetched dump, manifest and media, by name, into backups/
    _dump_set "$1"
    _backups_run /usr/bin/find "$BACKUPS_DIR/.offsite/backups" -mindepth 1 -maxdepth 1 \
        "${DUMP_SET[@]}" -exec /usr/bin/mv -t "$BACKUPS_DIR" -- '{}' +
}

_backups_drop_offsite() { _backups_run /usr/bin/rm -rf -- "$BACKUPS_DIR/.offsite"; }

# import DIR BASE: the drill's files fetched by hand, copied in and, under a rootless daemon,
# given to the containers' uid (the backup container reads them), the owner a dump has anyway.
_backups_import() {
    local from
    _dump_set "$2"
    from=$(readlink -f -- "$1") && [ -d "$from" ] || die "$1 is not a directory"
    detect_daemon
    _backups_run /usr/bin/find "$from" -mindepth 1 -maxdepth 1 "${DUMP_SET[@]}" \
        -exec /usr/bin/cp -a -t "$BACKUPS_DIR" -- '{}' +
    if [ "$DAEMON_KIND" = rootless ]; then
        _backups_run /usr/bin/find "$BACKUPS_DIR" -mindepth 1 -maxdepth 1 "${DUMP_SET[@]}" \
            -exec /usr/bin/chown -R --no-dereference "$CONTAINER_UID:$CONTAINER_GID" -- '{}' +
    fi
}

_backups_wipe_dumps() {
    _backups_run /usr/bin/find "$BACKUPS_DIR" -mindepth 1 -maxdepth 1 -name 'gmat-*' \
        -exec /usr/bin/rm -rf -- '{}' +
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

# Replaces the application database with a dump of backups/, read through backups_do: a new
# database, the role bootstrap (grants on the database and schema), the restore as the owner role
# with no owners or privileges from the dump, the bootstrap again (grants on what was restored).
# The dump is reached before the database is dropped. Any error stops the restore.
restore_database() { # restore_database DUMP_BASE
    backups_do exists dump "$1" || die "backups/$1.dump is not there"
    compose exec -T db sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" exec psql -X -q -v ON_ERROR_STOP=1 \
        -h 127.0.0.1 -U "$POSTGRES_USER" -d postgres -v db="$POSTGRES_DB"' <<'SQL'
DROP DATABASE IF EXISTS :"db" WITH (FORCE);
CREATE DATABASE :"db" TEMPLATE template0 ENCODING 'UTF8';
SQL
    restore_roles
    backups_do read dump "$1" | compose exec -T db sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" exec \
        pg_restore -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
        --role="$GMAT_DB_OWNER_USER" --no-owner --no-privileges --exit-on-error'
    restore_roles
    printf 'ANALYZE;\n' | db_sql
}

# The attachments a dump's snapshot listed, copied next to it by backup.sh. A file already on
# the volume is the same file: its name is its key, and stored files never change. A dump with no
# media directory has none to restore; one that cannot be checked stops the restore.
restore_media() { # restore_media DUMP_BASE
    backups_do exists media "$1" || return 0
    compose run --rm --no-deps -T -v "$PWD/backups/$1.media:/restored-media:ro" \
        --entrypoint sh web -c 'cp -rp --update=none /restored-media/. /srv/media/files/' </dev/null
}

verify_dump() { # verify_dump DUMP_BASE: checksum, restore into a scratch cluster, manifest
    compose run --rm --no-deps -T --entrypoint restore backup --verify "$1.dump" </dev/null
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
