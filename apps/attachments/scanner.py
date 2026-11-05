"""The malware scan of an upload, before anything else touches its bytes (M8.3, ADR-0015).

``clamd`` runs in its own container with no network, reached through a unix socket on a volume
only ``web`` and it mount. This module speaks its ``INSTREAM`` protocol: the file in chunks,
each with its length, a zero length at the end, one reply line.

It fails closed: a scanner that does not answer means the file is not stored. ``scan`` does
nothing when ``ATTACHMENT_SCAN_SOCKET`` is empty, which only development and the tests may leave
so (``gmat.E022`` refuses it in production).
"""

import socket
import struct
import time
from pathlib import Path

from django.conf import settings

CHUNK = 64 * 1024
FOUND = b" FOUND"
CLEAN = b"stream: OK"
PING_TIMEOUT = 5.0


class Infected(Exception):
    """The scanner found something; ``signature`` names it."""

    def __init__(self, signature: str) -> None:
        super().__init__(signature)
        self.signature = signature


class ScannerUnavailable(Exception):
    """No verdict: nothing listens, or it failed or took too long."""


def _left(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise ScannerUnavailable("The scan took too long")
    return left


def _reply(connection: socket.socket, deadline: float) -> bytes:
    """One reply, up to its terminating NUL or the end of the connection."""
    reply = b""
    while not reply.endswith(b"\0"):
        connection.settimeout(_left(deadline))
        data = connection.recv(4096)
        if not data:
            break
        reply += data
    return reply.rstrip(b"\0")


def _send_file(connection: socket.socket, path: Path, deadline: float) -> None:
    connection.sendall(b"zINSTREAM\0")
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK):
            connection.settimeout(_left(deadline))
            connection.sendall(struct.pack("!L", len(chunk)) + chunk)
    connection.settimeout(_left(deadline))
    connection.sendall(struct.pack("!L", 0))


def _verdict(reply: bytes) -> None:
    if reply == CLEAN:
        return
    if reply.endswith(FOUND):
        name = reply.removesuffix(FOUND).removeprefix(b"stream: ")
        raise Infected(name.decode("ascii", "replace")[:200])
    raise ScannerUnavailable(f"Unexpected reply: {reply[:80]!r}")


def scan(path: Path) -> None:
    """Return if ``path`` is clean; raise ``Infected`` or ``ScannerUnavailable`` otherwise."""
    address = settings.ATTACHMENT_SCAN_SOCKET
    if not address:
        return
    deadline = time.monotonic() + settings.ATTACHMENT_SCAN_TIMEOUT
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(_left(deadline))
            connection.connect(address)
            _send_file(connection, path, deadline)
            reply = _reply(connection, deadline)
    except OSError as exc:
        raise ScannerUnavailable(type(exc).__name__) from exc
    _verdict(reply)


def ping() -> None:
    """Raise ``ScannerUnavailable`` unless the scanner answers (``manage verify_internal``)."""
    address = settings.ATTACHMENT_SCAN_SOCKET
    if not address:
        return
    deadline = time.monotonic() + PING_TIMEOUT
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(PING_TIMEOUT)
            connection.connect(address)
            connection.sendall(b"zPING\0")
            reply = _reply(connection, deadline)
    except OSError as exc:
        raise ScannerUnavailable(type(exc).__name__) from exc
    if reply != b"PONG":
        raise ScannerUnavailable(f"Unexpected reply: {reply[:80]!r}")
