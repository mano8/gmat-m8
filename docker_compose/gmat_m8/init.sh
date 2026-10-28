#!/usr/bin/env bash
# Prepares the gmat-m8 compose stack (plan §7.3).
#
#   bash init.sh                    create .env and app.env from the *.example templates,
#                                   generate every secret, set mode 0600, create backups/ and flags/
#   bash init.sh --ci               the same, for a CI runner: backups/ is world-writable so the
#                                   container user (uid 1000) can write whatever uid the runner has
#   bash init.sh --reset-db [--yes] stop the stack and DELETE the database volume (asks first)
#
# Existing env files are never overwritten. Secrets come from /dev/urandom and are written
# only to the env files; this script prints no secret.
set -euo pipefail
umask 077
cd "$(dirname "${BASH_SOURCE[0]}")"

ci=0
reset_db=0
assume_yes=0
for arg in "$@"; do
    case "$arg" in
        --ci) ci=1 ;;
        --reset-db) reset_db=1 ;;
        --yes) assume_yes=1 ;;
        -h | --help)
            sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "init.sh: unknown option '$arg' (try --help)" >&2
            exit 64
            ;;
    esac
done

# Random material --------------------------------------------------------------------------
# $1 = number of random bytes; URL-safe base64, no padding.
random_b64url() {
    head -c "$1" /dev/urandom | base64 | tr -d '\n=' | tr '+/' '-_'
}

# Passwords and keys are redrawn until they avoid the markers the application and the role
# bootstrap refuse as "placeholders" (a random value can contain one by chance).
looks_like_placeholder() {
    printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | grep -qE \
        'change|placeholder|example|password|secret|replace|xxxx|dev-|test-|build-|insecure|^your-'
}

generate() {
    local kind="$1" value
    while :; do
        case "$kind" in
            __GENERATE_SECRET__) value=$(random_b64url 48) ;;
            __GENERATE_PASSWORD__) value=$(random_b64url 24) ;;
            # A Fernet key is the URL-safe base64 of 32 random bytes, padding included.
            __GENERATE_FERNET__) value=$(head -c 32 /dev/urandom | base64 | tr -d '\n' | tr '+/' '-_') ;;
            *)
                echo "init.sh: unknown generator '$kind'" >&2
                exit 1
                ;;
        esac
        looks_like_placeholder "$value" || break
    done
    printf '%s' "$value"
}

# Replaces every __GENERATE_*__ value of a file in place, one fresh value per occurrence.
fill_generated() {
    local file="$1" tmp line key value
    tmp=$(mktemp "${file}.XXXXXX")
    while IFS= read -r line || [ -n "$line" ]; do
        if [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(__GENERATE_[A-Z]+__)$ ]]; then
            key="${BASH_REMATCH[1]}"
            value=$(generate "${BASH_REMATCH[2]}")
            printf '%s=%s\n' "$key" "$value"
        else
            printf '%s\n' "$line"
        fi
    done <"$file" >"$tmp"
    mv "$tmp" "$file"
}

env_value() { # env_value FILE KEY
    sed -n "s/^$2=//p" "$1" | head -n 1
}

# --reset-db ------------------------------------------------------------------------------
if [ "$reset_db" = 1 ]; then
    if [ ! -f .env ]; then
        echo "init.sh: no .env here; nothing to reset" >&2
        exit 1
    fi
    project="gmat-m8"
    if [ "$assume_yes" != 1 ]; then
        printf 'This DELETES the database volume of project "%s" (all data). Type the project name to continue: ' "$project"
        read -r answer
        [ "$answer" = "$project" ] || { echo "init.sh: aborted" >&2; exit 1; }
    fi
    docker compose down --remove-orphans
    docker volume rm "${project}_pg_data" >/dev/null 2>&1 || true
    echo "init.sh: database volume removed; run 'docker compose up -d --build --wait' to start fresh"
    exit 0
fi

# Env files ---------------------------------------------------------------------------------
created=()
if [ ! -f .env ]; then
    cp .env.example .env
    fill_generated .env
    created+=(.env)
fi

if [ ! -f app.env ]; then
    cp app.env.example app.env
    fill_generated app.env
    domain=$(env_value .env APP_DOMAIN)
    https_port=$(env_value .env HTTPS_PORT)
    origin="https://${domain:-localhost}"
    if [ -n "${https_port:-}" ] && [ "$https_port" != "443" ]; then
        origin="${origin}:${https_port}"
    fi
    sed -i "s|^DJANGO_ALLOWED_HOSTS=__FROM_ENV_HOSTS__$|DJANGO_ALLOWED_HOSTS=${domain:-localhost}|" app.env
    sed -i "s|^DJANGO_CSRF_TRUSTED_ORIGINS=__FROM_ENV_ORIGINS__$|DJANGO_CSRF_TRUSTED_ORIGINS=${origin}|" app.env
    created+=(app.env)
fi

if cat .env app.env | grep -vE '^[[:space:]]*#' | grep -qE '__(GENERATE|FROM_ENV)_'; then
    echo "init.sh: an env file still contains an unfilled template value" >&2
    exit 1
fi
chmod 0600 .env app.env

# Directories -------------------------------------------------------------------------------
# backups/ is written by the backup container (uid 1000), flags/ is read by Caddy.
mkdir -p backups flags
chmod 0700 backups
chmod 0755 flags
if [ "$ci" = 1 ]; then
    # A CI runner is not uid 1000; the data is throwaway.
    chmod 0777 backups
fi

if [ "${#created[@]}" -gt 0 ]; then
    echo "init.sh: created ${created[*]} (mode 0600, generated secrets; never commit them)"
else
    echo "init.sh: env files already exist and were kept"
fi
cat <<'EOF'

Next:
  docker compose up -d --build --wait        # https://localhost:8443
  (development)  docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build
EOF
