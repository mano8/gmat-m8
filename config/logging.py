"""Structured logging with secret redaction (plan §5, "Logging and secret redaction").

Tokens travel in URLs (password-reset keys, invitation and email-change links) and
from there in ``Referer`` and ``HX-Current-URL`` headers. Every handler carries
``RedactionFilter``, which rewrites token paths and query strings in messages, tracebacks
and the ``request`` attribute before anything is formatted. A system check refuses a
handler without it (``apps.core.checks``).
"""

import contextvars
import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from django.http import HttpRequest
from django.views.debug import SafeExceptionReporterFilter

REDACTED = "REDACTED"

# The request id of the current request (set by ``apps.core.middleware.RequestIdMiddleware``).
request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)

# The path segment after each prefix is a secret (the same prefixes as the Caddyfile's
# access-log filter).
TOKEN_PATH_PREFIXES: tuple[str, ...] = (
    "/accounts/password/reset/key/",
    "/invite/",
    "/account/email-change/",
)

# Paths whose POST bodies never appear in error reports (passwords, tokens).
SENSITIVE_PATH_PREFIXES: tuple[str, ...] = ("/accounts/", "/invite/", "/account/")

# Request headers that carry full URLs (and so tokens) or credentials.
URL_HEADERS = frozenset({"referer", "hx-current-url"})
CREDENTIAL_HEADERS = frozenset({"cookie", "authorization", "x-csrftoken"})

_URL = re.compile(r"""https?://[^\s"'<>]+|(?<![\w.:/])/[^\s"'<>]*""")
_ASSIGNMENT = re.compile(
    r"(?i)\b(token|key|password|passwd|secret|code|signature|sessionid|csrftoken)=([^&\s\"']+)"
)
_HOST = re.compile(r"^(https?://[^/]*)(.*)$", re.DOTALL)
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_OPAQUE = re.compile(r"[A-Za-z0-9_\-.~:%]{24,}")


def _is_opaque(segment: str) -> bool:
    """A long, random-looking path segment: a token unless it is a plain UUID or a slug."""
    if not _OPAQUE.fullmatch(segment) or _UUID.fullmatch(segment):
        return False
    return any(char.isdigit() or char.isupper() for char in segment)


def redact_url(url: str) -> str:
    """Drop the query string and fragment and replace token-bearing path segments."""
    base, has_query, _ = url.partition("?")
    base, has_fragment, _ = base.partition("#")
    match = _HOST.match(base)
    origin, path = match.groups() if match else ("", base)
    for prefix in TOKEN_PATH_PREFIXES:
        start = path.find(prefix)
        if start != -1:
            head = path[: start + len(prefix)]
            segment, slash, tail = path[start + len(prefix) :].partition("/")
            if segment:
                path = f"{head}{REDACTED}{slash}{tail}"
    path = "/".join(REDACTED if _is_opaque(part) else part for part in path.split("/"))
    suffix = f"?{REDACTED}" if has_query else ""
    if has_fragment:
        suffix += f"#{REDACTED}"
    return f"{origin}{path}{suffix}"


def redact_text(text: str) -> str:
    """Redact every URL, path and ``secret=value`` assignment found in free text."""
    text = _URL.sub(lambda match: redact_url(match.group(0)), text)
    return _ASSIGNMENT.sub(lambda match: f"{match.group(1)}={REDACTED}", text)


@dataclass(frozen=True)
class RedactedRequest:
    """What a log record keeps of a request: method and redacted path, never headers."""

    method: str
    path: str

    @classmethod
    def from_request(cls, request: Any) -> RedactedRequest:
        method = str(getattr(request, "method", "") or "")
        path = redact_url(str(getattr(request, "path", "") or ""))
        return cls(method=method, path=path)


class RedactionFilter(logging.Filter):
    """Redacts the message, the traceback and the request of every record (never drops one)."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_text(record.getMessage())
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact_text(record.exc_text)
        if record.stack_info:
            record.stack_info = redact_text(record.stack_info)
        request = getattr(record, "request", None)
        if request is not None and not isinstance(request, RedactedRequest):
            record.request = RedactedRequest.from_request(request)
        return True


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line. Only known, already redacted fields are written."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", None),
        }
        status = getattr(record, "status_code", None)
        if status is not None:
            payload["status"] = status
        request = getattr(record, "request", None)
        if isinstance(request, RedactedRequest):
            payload["method"] = request.method
            payload["path"] = request.path
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            payload["exception"] = record.exc_text
        if record.stack_info:
            payload["stack"] = record.stack_info
        return json.dumps(payload, ensure_ascii=False, default=str)


class RedactingExceptionReporterFilter(SafeExceptionReporterFilter):
    """Error reports (the DEBUG page, an optional error tracker) never carry tokens.

    POST bodies of authentication pages are hidden entirely, which also covers the
    third-party views that do not declare ``sensitive_post_parameters``; URLs and the
    headers that carry them are redacted.
    """

    def get_post_parameters(self, request: HttpRequest | None) -> Any:
        params = super().get_post_parameters(request)
        if request is not None and request.path.startswith(SENSITIVE_PATH_PREFIXES):
            return dict.fromkeys(params, self.cleansed_substitute)
        return params

    def get_safe_request_meta(self, request: HttpRequest) -> dict[str, Any]:
        meta = super().get_safe_request_meta(request)
        for key in ("HTTP_REFERER", "HTTP_HX_CURRENT_URL", "QUERY_STRING"):
            if key in meta:
                meta[key] = self.cleansed_substitute
        for key in ("PATH_INFO", "REQUEST_URI", "RAW_URI"):
            if isinstance(meta.get(key), str):
                meta[key] = redact_url(meta[key])
        return meta


_DROPPED_EVENT_KEYS = frozenset({"cookies", "data", "query_string", "vars", "env"})


def _scrub(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        scrubbed: dict[Any, Any] = {}
        for key, item in value.items():
            name = str(key).lower()
            if name in _DROPPED_EVENT_KEYS or name in URL_HEADERS or name in CREDENTIAL_HEADERS:
                continue
            scrubbed[key] = _scrub(item)
        return scrubbed
    if isinstance(value, list | tuple):
        return [_scrub(item) for item in value]
    return value


def scrub_event(event: dict[str, Any], hint: Any = None) -> dict[str, Any]:
    """``before_send`` hook for an optional error tracker (plan §5).

    Use it with ``send_default_pii=False``. It drops cookies, request bodies, query
    strings, local variables and the URL-carrying and credential headers, and redacts every
    remaining string with the logging rules.
    """
    scrubbed: dict[str, Any] = _scrub(event)
    return scrubbed
