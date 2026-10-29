#!/usr/bin/env bash
# Entry point of the gmat-m8 application image (plan §7.1).
#
#   web                     Gunicorn (gthread), the only long-running web process
#   runserver               Django's development server (the `dev` target only)
#   worker                  outbox sender + housekeeping (arrives with M2.5)
#   migrate                 migrate + createcachetable, run once by the `migrate` service
#   check                   manage.py check --deploy; any warning is a failure
#   migrate-plan            lists pending migrations, read only
#   verify-internal         deploy-time checks against web:8000 (arrives with M8.4)
#   post-restore-reconcile  access reconciliation after a restore (arrives with M8.4)
#   healthcheck             exit 0 when the local web process answers /healthz/
#   manage <args...>        any other management command
#
# Secrets arrive only through the environment; nothing is printed that could contain one.
set -euo pipefail

cd /app

not_yet() {
    echo "entrypoint: '$1' is introduced in $2 and is not available in this build." >&2
    exit 78
}

command="${1:-web}"
if [ "$#" -gt 0 ]; then
    shift
fi

case "$command" in
    web)
        exec gunicorn --config /app/docker/app/gunicorn.conf.py config.wsgi:application
        ;;
    runserver)
        exec python manage.py runserver 0.0.0.0:8000
        ;;
    worker)
        not_yet worker M2.5
        ;;
    migrate)
        python manage.py migrate --noinput
        # The default cache is the security cache table (rate limits); it must exist
        # before the first request, and the owner role creates it.
        exec python manage.py createcachetable
        ;;
    check)
        exec python manage.py check --deploy --fail-level WARNING
        ;;
    migrate-plan)
        exec python manage.py migrate --plan
        ;;
    verify-internal)
        not_yet verify-internal M8.4
        ;;
    post-restore-reconcile)
        not_yet post-restore-reconcile M8.4
        ;;
    healthcheck)
        # No curl in the image. Django validates the Host header, so send the first allowed
        # host; /healthz/ is exempt from the HTTPS redirect.
        exec python - <<'PY'
import os
import sys
import urllib.request

host = os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost").split(",")[0].strip() or "localhost"
request = urllib.request.Request("http://127.0.0.1:8000/healthz/", headers={"Host": host})
try:
    with urllib.request.urlopen(request, timeout=4) as response:  # noqa: S310 (fixed http URL)
        sys.exit(0 if response.status == 200 else 1)
except Exception:
    sys.exit(1)
PY
        ;;
    manage)
        exec python manage.py "$@"
        ;;
    *)
        echo "entrypoint: unknown command '$command'" >&2
        echo "usage: entrypoint.sh {web|runserver|worker|migrate|check|migrate-plan|verify-internal|post-restore-reconcile|healthcheck|manage <args>}" >&2
        exit 64
        ;;
esac
