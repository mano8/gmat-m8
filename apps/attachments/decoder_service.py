"""The image decoder as a service of its own (M8.3, ADR-0015).

Decoding an image someone uploaded is the riskiest parsing the application does, so production
does it apart from ``web``: a container with its own uid (1001), no network, no volume but this
service's socket, and none of the application's secrets in its environment. An exploit in the
decoder finds nothing to read there and no way to send it anywhere.

For each request this process writes the bytes to a private directory and runs the decoder
(``decoder.py``) as a child under its resource limits and a wall-clock kill, as ``processing``
does outside production; it parses nothing of the image itself. The protocol, one request per
connection on a unix socket:

- to the service: one line of JSON (``ping``, or the format, the limits and the ``size``), then
  ``size`` bytes;
- from it: one byte, the decoder's exit status, then the WebP file if that status is 0.

Run as ``python -I decoder_service.py SOCKET``, so nothing of the project, the current directory
or the environment is imported: only the standard library here, and Pillow in the child.
``--ping SOCKET`` is the container's health check.
"""

import json
import os
import shutil
import signal
import socket
import socketserver
import stat
import subprocess  # nosec B404 - a fixed command: this interpreter and the decoder script
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

DECODER = Path(__file__).resolve().with_name("decoder.py")

EXIT_OK = 0
EXIT_UNDECODABLE = 13  # as decoder.py
EXIT_TIMEOUT = 124  # the child was killed at the deadline, or no slot came free in time
EXIT_REFUSED = 125  # a request this service will not run

FORMATS = frozenset({"JPEG", "PNG", "WEBP"})
LIMITS = ("max_pixels", "max_side", "max_bytes", "memory", "cpu")
MAX_HEADER = 4096
MAX_SOURCE = 16 * 1024 * 1024  # above the largest request ``web`` admits, whatever it says
MAX_TIMEOUT = 300
READ_TIMEOUT = 30.0
CONCURRENCY = 2  # the installation-wide bound on uploads in progress (ATTACHMENT_MAX_PROCESSING)
PING_TIMEOUT = 3.0
CHUNK = 64 * 1024


def _valid(request: dict[str, Any]) -> bool:
    """Only integers reach the child's command line, and only the formats it knows."""
    numbers = [request.get(name) for name in (*LIMITS, "size", "timeout")]
    if not all(type(value) is int and value > 0 for value in numbers):
        return False
    return (
        request.get("format") in FORMATS
        and request["size"] <= MAX_SOURCE
        and request["timeout"] <= MAX_TIMEOUT
    )


def command_for(request: dict[str, Any], source: Path, target: Path) -> list[str]:
    limits = [str(request[name]) for name in LIMITS]
    return [
        sys.executable,
        "-I",
        str(DECODER),
        str(source),
        str(target),
        request["format"],
        *limits,
    ]


def run_decoder(request: dict[str, Any], source: Path, target: Path) -> int:
    """The child's exit status; the same clean environment as ``processing``'s child."""
    try:
        result = subprocess.run(  # noqa: S603  # nosec B603 - see command_for
            command_for(request, source, target),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=request["timeout"],
            check=False,
            env={"LANG": "C.UTF-8"},
        )
    except subprocess.TimeoutExpired:
        return EXIT_TIMEOUT
    return result.returncode if 0 <= result.returncode < 256 else EXIT_UNDECODABLE


class Handler(socketserver.StreamRequestHandler):
    timeout = READ_TIMEOUT
    server: DecoderServer

    def _header(self) -> dict[str, Any] | None:
        line = self.rfile.readline(MAX_HEADER + 1)
        if len(line) > MAX_HEADER or not line.endswith(b"\n"):
            return None
        try:
            request = json.loads(line)
        except ValueError:
            return None
        return request if isinstance(request, dict) else None

    def _receive(self, path: Path, size: int) -> bool:
        left = size
        with path.open("wb") as stream:
            while left:
                data = self.rfile.read(min(CHUNK, left))
                if not data:
                    return False
                stream.write(data)
                left -= len(data)
        return True

    def _answer(self, status: int, result: Path | None = None) -> None:
        self.wfile.write(bytes([status]))
        if result is not None:
            with result.open("rb") as stream:
                shutil.copyfileobj(stream, self.wfile, CHUNK)

    def _convert(self, request: dict[str, Any], directory: Path) -> None:
        source, target = directory / "in", directory / "out"
        if not self._receive(source, request["size"]):
            self._answer(EXIT_REFUSED)
            return
        if not self.server.slots.acquire(timeout=READ_TIMEOUT):
            self._answer(EXIT_TIMEOUT)
            return
        try:
            status = run_decoder(request, source, target)
        finally:
            self.server.slots.release()
        self._answer(status, target if status == EXIT_OK else None)

    def handle(self) -> None:
        request = self._header()
        if request is None:
            self._answer(EXIT_REFUSED)
        elif request.get("ping") is True:
            self._answer(EXIT_OK)
        elif not _valid(request):
            self._answer(EXIT_REFUSED)
        else:
            with tempfile.TemporaryDirectory(prefix="decode-") as directory:
                self._convert(request, Path(directory))


class DecoderServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, address: str) -> None:
        self.slots = threading.BoundedSemaphore(CONCURRENCY)
        _remove_stale(address)
        super().__init__(address, Handler)
        # The directory is a volume only this service and ``web`` (another uid) mount.
        os.chmod(address, 0o666)  # noqa: S103  # nosec B103

    def handle_error(self, request: Any, client_address: Any) -> None:
        """A client that went away: one line, nothing of what it sent."""
        print(f"decoder: {sys.exc_info()[0]}", file=sys.stderr)


def _remove_stale(address: str) -> None:
    """A socket left by the previous run; anything else at that path is a mistake to refuse."""
    try:
        mode = os.stat(address).st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(mode):
        raise SystemExit(f"{address} exists and is not a socket")
    os.unlink(address)


def ping(address: str) -> bool:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(PING_TIMEOUT)
            connection.connect(address)
            connection.sendall(b'{"ping": true}\n')
            return connection.recv(1) == bytes([EXIT_OK])
    except OSError:
        return False


def main(arguments: list[str]) -> int:
    if len(arguments) == 2 and arguments[0] == "--ping":
        return 0 if ping(arguments[1]) else 1
    if len(arguments) != 1:
        print("usage: decoder_service.py [--ping] SOCKET", file=sys.stderr)
        return 64
    # As PID 1 a signal without a handler is ignored, and `docker stop` would wait to kill.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    with DecoderServer(arguments[0]) as server:
        server.serve_forever()
    return 0  # pragma: no cover - serve_forever only ends with the process


if __name__ == "__main__":  # pragma: no cover - runs in the decoder's container
    sys.exit(main(sys.argv[1:]))
