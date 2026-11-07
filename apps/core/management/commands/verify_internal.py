"""``entrypoint.sh verify-internal``: is the new release really up? (plan §9, deploy step 9).

Runs in a one-off container on the ``edge`` network, against ``web:8000`` directly: the public
site is still behind the maintenance gate. It asks for the liveness and readiness endpoints,
the login page and a hashed static asset, then whether the malware scanner and the image decoder
answer (without them an upload is refused, so a release is not whole), and writes nothing: a
commit point opens the gates only after these pass. ``check --deploy`` and ``migrate --check``
run in the entrypoint.
"""

import http.client
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.staticfiles.storage import staticfiles_storage
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.urls import reverse

from apps.attachments import decoder_client, scanner

DEFAULT_TARGET = "http://web:8000"
ASSET = "dist/app.css"
TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class Answer:
    status: int
    content_type: str
    length: int


def fetch(target: str, path: str, host: str) -> Answer:
    """One GET as Caddy would send it: the public host name, and TLS already terminated."""
    parts = urlsplit(target)
    connection = http.client.HTTPConnection(
        parts.hostname or "web", parts.port or 80, timeout=TIMEOUT_SECONDS
    )
    try:
        connection.request("GET", path, headers={"Host": host, "X-Forwarded-Proto": "https"})
        response = connection.getresponse()
        return Answer(response.status, response.getheader("Content-Type", ""), len(response.read()))
    finally:
        connection.close()


def _problem(name: str, path: str, target: str, host: str, wanted_type: str) -> str | None:
    try:
        answer = fetch(target, path, host)
    except OSError as exc:
        return f"{name}: no answer ({type(exc).__name__})"
    if answer.status != 200:
        return f"{name}: HTTP {answer.status}"
    if wanted_type not in answer.content_type or not answer.length:
        return f"{name}: unexpected response ({answer.content_type or 'no type'})"
    return None


def _upload_problems() -> list[str]:
    problems = []
    try:
        scanner.ping()
    except scanner.ScannerUnavailable as exc:
        problems.append(f"malware scanner: no answer ({exc})")
    if not decoder_client.ping():
        problems.append("image decoder: no answer")
    return problems


class Command(BaseCommand):
    help = "Check the running web process directly, bypassing the maintenance gate."
    requires_system_checks: Any = []

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--target", default=DEFAULT_TARGET, help="Where web listens")

    def handle(self, *args: Any, **options: Any) -> None:
        target = options["target"]
        host = settings.ALLOWED_HOSTS[0] if settings.ALLOWED_HOSTS else "localhost"
        checks = (
            ("healthz", reverse("core:healthz"), "json"),
            ("readyz", reverse("core:readyz"), "json"),
            ("login page", reverse("account_login"), "html"),
            ("static asset", staticfiles_storage.url(ASSET), "css"),
        )
        problems = [
            problem
            for name, path, wanted in checks
            if (problem := _problem(name, path, target, host, wanted)) is not None
        ]
        problems += _upload_problems()
        if problems:
            raise CommandError("verify-internal failed: " + "; ".join(problems))
        self.stdout.write(f"verify-internal: {len(checks) + 2} checks passed against {target}")
