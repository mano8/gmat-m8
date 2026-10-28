"""Gunicorn settings for the gmat-m8 `web` service (plan §7.1).

Caddy is the only HTTP access log (it redacts token URLs, ``Referer`` and ``HX-Current-URL``),
so Gunicorn's access log stays off. Gunicorn's own error log goes through the same JSON
formatter and redaction filter as Django's, so a malformed request line that names a token
URL is still redacted.
"""

import os
from typing import Any

bind = "0.0.0.0:8000"

# gthread: a few processes, a few threads each. Environment overrides exist for sizing only.
worker_class = "gthread"
workers = int(os.environ.get("GUNICORN_WORKERS", "3"))
threads = int(os.environ.get("GUNICORN_THREADS", "4"))

# The timeout is a worker heartbeat, not a request limit.
timeout = 60
graceful_timeout = 30
keepalive = 5
max_requests = 2000
max_requests_jitter = 200

# Heartbeat files live in shared memory: the root filesystem is read-only.
worker_tmp_dir = "/dev/shm"  # noqa: S108

# Only Caddy, on the pinned `edge` subnet, may set forwarded headers. FORWARDED_ALLOW_IPS is
# read by Gunicorn itself; a wildcard would let any client spoof them, so refuse to start.
_forwarded = os.environ.get("FORWARDED_ALLOW_IPS", "")
if _forwarded.strip() in {"", "*"} or "*" in _forwarded:
    raise SystemExit("FORWARDED_ALLOW_IPS must list the edge subnet (never '*' or empty)")
forwarded_allow_ips = _forwarded

accesslog = None

# No runtime control socket: it would need a writable home and adds a management surface.
control_socket_disable = True
errorlog = "-"
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")
logconfig_dict: dict[str, Any] = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {"redact": {"()": "config.logging.RedactionFilter"}},
    "formatters": {"json": {"()": "config.logging.JsonFormatter"}},
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stderr",
            "formatter": "json",
            "filters": ["redact"],
        },
    },
    "loggers": {
        "gunicorn.error": {
            "level": loglevel.upper(),
            "handlers": ["console"],
            "propagate": False,
        },
    },
}
