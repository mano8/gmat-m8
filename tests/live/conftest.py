"""Fixtures for the live layer: tests against a running compose stack (plan §8.1, layer C).

The stack is started by the caller (``compose-smoke.yaml``, or by hand with ``bash init.sh &&
docker compose up -d --build --wait`` in ``docker_compose/gmat_m8``). These tests never start
or build it; they only use ``docker compose`` to look inside it and to flip its switches.

Environment:

- ``LIVE_BASE_URL``      public address of the stack (default ``https://localhost:8443``);
- ``LIVE_COMPOSE_DIR``   the compose directory (default ``docker_compose/gmat_m8``);
- ``LIVE_COMPOSE_FILES`` space-separated ``-f`` files (default: none, so compose reads
  ``COMPOSE_FILE`` from the stack's ``.env``, as ``docker compose up`` did);
- ``LIVE_REQUIRED=1``    a stack that does not answer fails the run instead of skipping it.
"""

import http.client
import os
import shlex
import ssl
import subprocess
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Response:
    status: int
    headers: list[tuple[str, str]]
    body: bytes

    def header(self, name: str) -> str | None:
        wanted = name.lower()
        for key, value in self.headers:
            if key.lower() == wanted:
                return value
        return None

    def headers_named(self, name: str) -> list[str]:
        wanted = name.lower()
        return [value for key, value in self.headers if key.lower() == wanted]

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


@dataclass
class Stack:
    base_url: str
    compose_dir: Path
    compose_files: list[str]
    host: str = field(init=False)
    port: int = field(init=False)
    scheme: str = field(init=False)

    def __post_init__(self) -> None:
        parts = urlsplit(self.base_url)
        self.scheme = parts.scheme
        self.host = parts.hostname or "localhost"
        self.port = parts.port or (443 if parts.scheme == "https" else 80)

    # --- HTTP through Caddy ------------------------------------------------------------
    def request(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        timeout: float = 20.0,
    ) -> Response:
        """One request on a fresh connection (the certificate is Caddy's internal CA)."""
        if self.scheme == "https":
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            connection: http.client.HTTPConnection = http.client.HTTPSConnection(
                self.host, self.port, timeout=timeout, context=context
            )
        else:
            connection = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        try:
            connection.request(method, path, body=body, headers=dict(headers or {}))
            raw = connection.getresponse()
            return Response(raw.status, raw.getheaders(), raw.read())
        finally:
            connection.close()

    def get(self, path: str, **kwargs: object) -> Response:
        return self.request("GET", path, **kwargs)  # type: ignore[arg-type]

    # --- docker compose ------------------------------------------------------------------
    def compose(
        self, *args: str, check: bool = True, timeout: float = 300.0
    ) -> subprocess.CompletedProcess[str]:
        command = ["docker", "compose"]
        for file in self.compose_files:
            command += ["-f", file]
        command += list(args)
        result = subprocess.run(  # noqa: S603 (fixed docker invocation, test input only)
            command,
            cwd=self.compose_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if check and result.returncode != 0:
            raise AssertionError(
                f"{shlex.join(command)} failed ({result.returncode}):\n"
                f"{result.stdout}\n{result.stderr}"
            )
        return result

    def sql(self, statement: str) -> str:
        """Run SQL as the bootstrap superuser inside the db container."""
        result = self.compose(
            "exec",
            "-T",
            "db",
            "sh",
            "-c",
            'PGPASSWORD="$POSTGRES_PASSWORD" psql -h 127.0.0.1 -U "$POSTGRES_USER" '
            '-d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -tA -c "$0"',
            statement,
        )
        return result.stdout.strip()

    def reset_rate_limits(self) -> None:
        self.sql("DELETE FROM django_cache")

    def env_value(self, name: str, default: str = "") -> str:
        """A value from the compose ``.env`` file (the stack's own configuration)."""
        path = self.compose_dir / ".env"
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip() or default
        return default

    def logs(self, *services: str) -> str:
        result = self.compose("logs", "--no-color", "--no-log-prefix", *services)
        return result.stdout + result.stderr

    def wait_healthy(self, *services: str, timeout: float = 120.0) -> None:
        self.compose("up", "-d", "--wait", "--wait-timeout", str(int(timeout)), *services)


@pytest.fixture(scope="session")
def stack() -> Iterator[Stack]:
    compose_dir = Path(os.environ.get("LIVE_COMPOSE_DIR", ROOT / "docker_compose" / "gmat_m8"))
    files = os.environ.get("LIVE_COMPOSE_FILES", "").split()
    candidate = Stack(
        base_url=os.environ.get("LIVE_BASE_URL", "https://localhost:8443"),
        compose_dir=compose_dir,
        compose_files=files,
    )
    # A required stack may still be settling; an absent one should not slow a plain `pytest`.
    required = os.environ.get("LIVE_REQUIRED") == "1"
    deadline = time.monotonic() + (60 if required else 3)
    last_error = ""
    while time.monotonic() < deadline:
        try:
            if candidate.get("/healthz/", timeout=5).status == 200:
                yield candidate
                return
        except OSError as error:
            last_error = str(error)
        time.sleep(1)
    message = f"no compose stack answers at {candidate.base_url} ({last_error})"
    if required:
        pytest.fail(message)
    pytest.skip(message)
