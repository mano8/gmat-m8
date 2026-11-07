#!/usr/bin/env bash
# Entry point of the gmat-m8 application image (plan §7.1).
#
#   web                     Gunicorn (gthread), the only long-running web process
#   runserver               Django's development server (the `dev` target only)
#   worker                  outbox sender + housekeeping (honours flags/worker.paused, OUTBOUND_EMAIL)
#   worker-healthcheck      exit 0 when the worker's heartbeat is recent (paused or not;
#                           WORKER_EXPECT_STATE=paused|running,... also checks its state)
#   migrate                 migrate + createcachetable, run once by the `migrate` service
#   check                   manage.py check --deploy; any warning is a failure
#   migrate-plan            lists pending migrations, read only
#   verify-internal         deploy-time checks against web:8000, bypassing the gate (plan §9)
#   post-restore-reconcile  security, MFA and outbox reconciliation after a restore (plan §9)
#   healthcheck             exit 0 when the local web process answers /healthz/
#   decoder                 the image decoder service: its own container, uid, no network
#   decoder-healthcheck     exit 0 when the decoder service answers
#   manage <args...>        any other management command
#
# Secrets arrive only through the environment; nothing is printed that could contain one.
set -euo pipefail

cd /app

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
        exec python manage.py worker
        ;;
    worker-healthcheck)
        # The worker rewrites its heartbeat every loop, also while paused; a send lasts at most
        # OUTBOX_SEND_DEADLINE (60 s). No Django import: this runs every 30 s.
        exec python - <<'PY'
import json
import os
import sys
import time

path = os.environ.get("WORKER_HEARTBEAT_FILE", "/tmp/gmat-worker.heartbeat")
max_age = float(os.environ.get("WORKER_HEARTBEAT_MAX_AGE", "120"))
expected = [state for state in os.environ.get("WORKER_EXPECT_STATE", "").split(",") if state]
try:
    with open(path, encoding="utf-8") as handle:
        beat = json.load(handle)
    fresh = time.time() - float(beat["at"]) <= max_age
    sys.exit(0 if fresh and (not expected or beat.get("state") in expected) else 1)
except Exception:
    sys.exit(1)
PY
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
        python manage.py check --deploy --fail-level WARNING
        python manage.py migrate --check
        exec python manage.py verify_internal "$@"
        ;;
    post-restore-reconcile)
        exec python manage.py post_restore_reconcile "$@"
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
    decoder)
        # -I: nothing of the project, the current directory or the environment is imported.
        exec python -I /app/apps/attachments/decoder_service.py "${ATTACHMENT_DECODER_SOCKET:?}"
        ;;
    decoder-healthcheck)
        exec python -I /app/apps/attachments/decoder_service.py --ping "${ATTACHMENT_DECODER_SOCKET:?}"
        ;;
    manage)
        exec python manage.py "$@"
        ;;
    *)
        echo "entrypoint: unknown command '$command'" >&2
        echo "usage: entrypoint.sh {web|runserver|worker|worker-healthcheck|migrate|check|migrate-plan|verify-internal|post-restore-reconcile|healthcheck|decoder|decoder-healthcheck|manage <args>}" >&2
        exit 64
        ;;
esac
