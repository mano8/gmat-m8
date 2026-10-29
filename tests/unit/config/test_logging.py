"""Logging and secret redaction (plan §5): rules, filter, formatter, and the sentinel test."""

import io
import json
import logging
import sys
from collections.abc import Callable, Iterator

import pytest
from django.test import Client, RequestFactory, override_settings
from django.urls import reverse

from apps.accounts.models import User
from config.logging import (
    REDACTED,
    JsonFormatter,
    RedactedRequest,
    RedactingExceptionReporterFilter,
    RedactionFilter,
    RequestIdFilter,
    redact_text,
    redact_url,
    request_id_var,
    scrub_event,
)

# A recognisable secret shaped like a real token (43 url-safe characters).
SENTINEL = "Snt1nel-Q7x2Lp9Zr4Vm8Kc3Wd6Hj0Tb5Ny1Fg2Ue7S"


class TestRules:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("/healthz/", "/healthz/"),
            ("/accounts/login/?next=/admin/", f"/accounts/login/?{REDACTED}"),
            ("/a/#frag", f"/a/#{REDACTED}"),
            (
                f"/accounts/password/reset/key/0190abcd-{SENTINEL}/",
                f"/accounts/password/reset/key/{REDACTED}/",
            ),
            (
                "/accounts/password/reset/key/0190abcd-set-password/",
                f"/accounts/password/reset/key/{REDACTED}/",
            ),
            (f"/invitations/{SENTINEL}/accept/", f"/invitations/{REDACTED}/accept/"),
            (
                f"https://app.example.org/email-change/{SENTINEL}/?x=1",
                f"https://app.example.org/email-change/{REDACTED}/?{REDACTED}",
            ),
            (
                "/projects/0190e3b2-7c1a-7d4e-9f00-112233445566/",
                "/projects/0190e3b2-7c1a-7d4e-9f00-112233445566/",
            ),
            ("/material-requests-overview-page/", "/material-requests-overview-page/"),
            ("/accounts/password/reset/key/", "/accounts/password/reset/key/"),
        ],
    )
    def test_redact_url(self, url: str, expected: str) -> None:
        assert redact_url(url) == expected

    def test_redact_text_finds_urls_and_assignments(self) -> None:
        text = (
            f'Not Found: /invitations/{SENTINEL}/ (from "https://x.example/r?token={SENTINEL}") '
            f"password={SENTINEL}&code=123456 at 2026/10/27"
        )
        redacted = redact_text(text)
        assert SENTINEL not in redacted
        assert "code=REDACTED" in redacted
        assert "2026/10/27" in redacted

    def test_redaction_is_idempotent(self) -> None:
        once = redact_text(f"/accounts/password/reset/key/1-{SENTINEL}/?a=b")
        assert redact_text(once) == once


class TestFilterAndFormatter:
    def make_record(self, msg: str, *args: object, **extra: object) -> logging.LogRecord:
        record = logging.LogRecord("django.request", logging.WARNING, __file__, 1, msg, args, None)
        for key, value in extra.items():
            setattr(record, key, value)
        return record

    def test_message_args_traceback_stack_and_request(self) -> None:
        try:
            raise RuntimeError(f"/invitations/{SENTINEL}/")
        except RuntimeError:
            exc_info = sys.exc_info()
        request = RequestFactory().get(f"/accounts/password/reset/key/1-{SENTINEL}/?x={SENTINEL}")
        record = self.make_record(
            "Not Found: %s", request.get_full_path(), request=request, status_code=404
        )
        record.exc_info = exc_info
        record.stack_info = f"Stack: /invitations/{SENTINEL}/"
        assert RedactionFilter().filter(record) is True
        assert SENTINEL not in record.getMessage()
        assert record.args is None
        assert record.exc_text and SENTINEL not in record.exc_text
        assert SENTINEL not in record.stack_info
        assert getattr(record, "request") == RedactedRequest(  # noqa: B009
            method="GET", path=f"/accounts/password/reset/key/{REDACTED}/"
        )
        RequestIdFilter().filter(record)
        output = json.loads(JsonFormatter().format(record))
        assert output["status"] == 404
        assert output["path"] == f"/accounts/password/reset/key/{REDACTED}/"
        assert output["level"] == "WARNING"
        assert SENTINEL not in json.dumps(output)

    def test_an_already_redacted_request_is_kept(self) -> None:
        redacted = RedactedRequest(method="GET", path="/x/")
        record = self.make_record("ok", request=redacted)
        RedactionFilter().filter(record)
        assert getattr(record, "request") is redacted  # noqa: B009

    def test_formatter_formats_an_unfiltered_traceback(self) -> None:
        try:
            raise ValueError("boom")
        except ValueError:
            record = self.make_record("failed")
            record.exc_info = sys.exc_info()
        record.stack_info = "stack"
        output = json.loads(JsonFormatter().format(record))
        assert "ValueError: boom" in output["exception"]
        assert output["stack"] == "stack"
        assert output["request_id"] is None

    def test_request_id_is_attached(self) -> None:
        token = request_id_var.set("abc123def456")
        try:
            record = self.make_record("hello")
            RequestIdFilter().filter(record)
        finally:
            request_id_var.reset(token)
        assert json.loads(JsonFormatter().format(record))["request_id"] == "abc123def456"


class TestErrorReports:
    def test_auth_post_bodies_and_url_headers_are_hidden(self) -> None:
        request = RequestFactory().post(
            "/accounts/login/?next=/x/",
            {"login": "a@example.org", "password": SENTINEL},
            HTTP_REFERER=f"https://app.example.org/invitations/{SENTINEL}/",
            HTTP_HX_CURRENT_URL=f"https://app.example.org/invitations/{SENTINEL}/",
        )
        reporter = RedactingExceptionReporterFilter()
        params = reporter.get_post_parameters(request)
        assert set(params.values()) == {reporter.cleansed_substitute}
        meta = reporter.get_safe_request_meta(request)
        assert SENTINEL not in json.dumps(meta, default=str)
        assert meta["QUERY_STRING"] == reporter.cleansed_substitute

    def test_other_post_bodies_follow_the_default_rules(self) -> None:
        request = RequestFactory().post("/projects/", {"name": "Hospital X"})
        params = RedactingExceptionReporterFilter().get_post_parameters(request)
        assert params["name"] == "Hospital X"
        assert RedactingExceptionReporterFilter().get_post_parameters(None) == {}

    def test_error_tracker_events_are_scrubbed(self) -> None:
        event = {
            "request": {
                "url": f"https://app.example.org/invitations/{SENTINEL}/?a=1",
                "query_string": f"token={SENTINEL}",
                "cookies": {"__Host-sessionid": SENTINEL},
                "data": {"password": SENTINEL},
                "headers": {
                    "Referer": SENTINEL,
                    "HX-Current-URL": SENTINEL,
                    "Authorization": SENTINEL,
                    "User-Agent": "pytest",
                },
            },
            "exception": {
                "values": [
                    {
                        "value": f"Not Found: /invitations/{SENTINEL}/",
                        "stacktrace": {"frames": [{"vars": {"token": SENTINEL}, "lineno": 3}]},
                    }
                ]
            },
            "breadcrumbs": ({"message": f"GET /invitations/{SENTINEL}/"},),
            "level": "error",
        }
        scrubbed = scrub_event(event, hint=None)
        assert SENTINEL not in json.dumps(scrubbed)
        assert scrubbed["request"]["headers"] == {"User-Agent": "pytest"}
        assert scrubbed["exception"]["values"][0]["stacktrace"]["frames"] == [{"lineno": 3}]
        assert scrubbed["level"] == "error"


@pytest.fixture
def captured_logs() -> Iterator[io.StringIO]:
    """Point the project's configured handlers at a buffer (the real filters stay)."""
    buffer = io.StringIO()
    handlers = {
        handler
        for name in ("", "django", "django.request", "django.server")
        for handler in logging.getLogger(name).handlers
        if isinstance(handler, logging.StreamHandler)
        and any(isinstance(item, RedactionFilter) for item in handler.filters)
    }
    assert handlers, "the configured redacting handler was not found"
    previous = {handler: handler.setStream(buffer) for handler in handlers}
    yield buffer
    for handler, stream in previous.items():
        handler.setStream(stream)


@pytest.mark.django_db
class TestSentinel:
    """Plant the sentinel everywhere a token travels; it must never reach a log line."""

    def test_sentinel_never_reaches_the_logs(
        self, captured_logs: io.StringIO, make_user: Callable[..., User]
    ) -> None:
        make_user()
        url_headers = {
            "Referer": f"https://app.example.org/accounts/password/reset/key/1-{SENTINEL}/",
            "HX-Current-URL": f"https://app.example.org/invitations/{SENTINEL}/",
        }
        client = Client(raise_request_exception=False)
        # Password-reset URL (valid shape, unknown token) and a query string.
        client.get(
            f"/accounts/password/reset/key/1-{SENTINEL}/?next={SENTINEL}", headers=url_headers
        )
        # Invitation and email-change URLs (their routes arrive in M2.6: 404s are logged).
        client.get(f"/invitations/{SENTINEL}/accept/", headers=url_headers)
        client.get(f"/accounts/email-change/{SENTINEL}/confirm/", headers=url_headers)
        # A password field, on a failed login.
        client.post(
            reverse("account_login"),
            {"login": "tech@example.org", "password": SENTINEL},
            headers=url_headers,
        )
        # A server error while handling a token URL: traceback and request are logged.
        with override_settings(ROOT_URLCONF="tests.unit.config.urls_raise"):
            response = client.get(f"/boom/{SENTINEL}/?token={SENTINEL}", headers=url_headers)
        assert response.status_code == 500

        output = captured_logs.getvalue()
        lines = [json.loads(line) for line in output.splitlines() if line.strip()]
        assert any(line.get("status") == 404 for line in lines)
        assert any(line.get("status") == 500 and "exception" in line for line in lines)
        assert all("request_id" in line for line in lines)
        assert SENTINEL not in output
