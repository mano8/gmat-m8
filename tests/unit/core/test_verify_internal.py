"""``verify-internal``: the deploy's check of ``web`` behind the maintenance gate (plan §9)."""

import http.server
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from django.contrib.staticfiles.storage import staticfiles_storage
from django.core.management import call_command
from django.core.management.base import CommandError
from django.urls import reverse

from tests.clamd import fake_clamd, refused_socket

CSS = "/static/dist/app.abc123.css"


@dataclass
class FakeWeb:
    url: str
    answers: dict[str, tuple[int, str, bytes]] = field(default_factory=dict)
    seen: list[dict[str, str]] = field(default_factory=list)


@pytest.fixture
def web(monkeypatch: pytest.MonkeyPatch, settings: Any) -> Iterator[FakeWeb]:
    settings.ALLOWED_HOSTS = ["gmat.example.org"]
    monkeypatch.setattr(staticfiles_storage, "url", lambda name: CSS)
    state = FakeWeb(url="")
    state.answers = {
        reverse("core:healthz"): (200, "application/json", b"{}"),
        reverse("core:readyz"): (200, "application/json", b"{}"),
        reverse("account_login"): (200, "text/html; charset=utf-8", b"<html>"),
        CSS: (200, "text/css", b"body{}"),
    }

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            state.seen.append(dict(self.headers))
            status, content_type, body = state.answers.get(self.path, (404, "text/html", b"no"))
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_address[1]}"
    yield state
    server.shutdown()
    thread.join()


def run(target: str) -> str:
    out = StringIO()
    call_command("verify_internal", "--target", target, stdout=out)
    return out.getvalue()


def test_a_healthy_web_passes_as_caddy_would_ask(web: FakeWeb) -> None:
    assert "6 checks passed" in run(web.url)
    assert {headers["Host"] for headers in web.seen} == {"gmat.example.org"}
    assert {headers["X-Forwarded-Proto"] for headers in web.seen} == {"https"}


@pytest.mark.parametrize(
    ("url_name", "answer", "named"),
    [
        ("core:healthz", (500, "text/html", b"oops"), "healthz: HTTP 500"),
        ("core:readyz", (503, "application/json", b"{}"), "readyz: HTTP 503"),
        ("account_login", (500, "text/html", b"oops"), "login page: HTTP 500"),
        ("account_login", (301, "text/html", b""), "login page: HTTP 301"),
        (None, (404, "text/html", b"gone"), "static asset: HTTP 404"),
        (None, (200, "text/html", b"<html>"), "static asset: unexpected"),
        (None, (200, "text/css", b""), "static asset: unexpected"),
        ("account_login", (200, "", b"x"), "no type"),
    ],
)
def test_each_failure_is_named(
    web: FakeWeb, url_name: str | None, answer: tuple[int, str, bytes], named: str
) -> None:
    web.answers[reverse(url_name) if url_name else CSS] = answer
    with pytest.raises(CommandError, match=named):
        run(web.url)


def test_no_answer_at_all(web: FakeWeb) -> None:
    with pytest.raises(CommandError, match="healthz: no answer"):
        run("http://127.0.0.1:9")


def test_no_allowed_host_falls_back_to_localhost(web: FakeWeb, settings: Any) -> None:
    settings.ALLOWED_HOSTS = []
    run(web.url)
    assert web.seen[0]["Host"] == "localhost"


class TestUploadServices:
    """M8.3: without a scanner or a decoder uploads are refused, so a release is not whole."""

    def test_both_answer(self, web: FakeWeb, settings: Any) -> None:
        with fake_clamd() as (address, _behaviour):
            settings.ATTACHMENT_SCAN_SOCKET = address
            assert "6 checks passed" in run(web.url)

    def test_a_scanner_that_does_not_answer_stops_the_deploy(
        self, web: FakeWeb, settings: Any, tmp_path: Path
    ) -> None:
        settings.ATTACHMENT_SCAN_SOCKET = refused_socket(tmp_path)
        with pytest.raises(CommandError, match="malware scanner"):
            run(web.url)

    def test_a_decoder_that_does_not_answer_stops_the_deploy(
        self, web: FakeWeb, settings: Any, tmp_path: Path
    ) -> None:
        settings.ATTACHMENT_DECODER_SOCKET = refused_socket(tmp_path)
        with pytest.raises(CommandError, match="image decoder"):
            run(web.url)
