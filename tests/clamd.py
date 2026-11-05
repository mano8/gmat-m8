"""A fake clamd on a unix socket: the tests' side of ``apps.attachments.scanner``.

It speaks the part of the protocol the application uses (``zPING``, ``zINSTREAM``) and decides
by the content. The real scanner is in ``tests/live`` (EICAR through the stack).
"""

import shutil
import socket
import socketserver
import struct
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

UNIX_SOCKETS = hasattr(socket, "AF_UNIX")  # not on a Windows development machine
MARKER = b"FAKE-MALWARE"
SIGNATURE = "Fake.Test.Signature"


@dataclass
class Behaviour:
    """What the fake answers; by default the truth about ``MARKER``."""

    reply: bytes | None = None  # always this, whatever was sent
    silent: bool = False  # reads, then never answers
    received: list[bytes] = field(default_factory=list)


class _Handler(socketserver.StreamRequestHandler):
    server: _Server

    def _stream(self) -> bytes:
        data = b""
        while True:
            header = self.rfile.read(4)
            if len(header) < 4:
                return data
            (size,) = struct.unpack("!L", header)
            if size == 0:
                return data
            data += self.rfile.read(size)

    def _answer(self, data: bytes) -> bytes:
        behaviour = self.server.behaviour
        if behaviour.reply is not None:
            return behaviour.reply
        if MARKER in data:
            return f"stream: {SIGNATURE} FOUND\0".encode()
        return b"stream: OK\0"

    def handle(self) -> None:
        command = b""
        while not command.endswith(b"\0"):
            byte = self.rfile.read(1)
            if not byte:
                return
            command += byte
        if command == b"zPING\0":
            self.wfile.write(self.server.behaviour.reply or b"PONG\0")
            return
        data = self._stream()
        self.server.behaviour.received.append(data)
        if self.server.behaviour.silent:
            self.server.release.wait(10)
            return
        self.wfile.write(self._answer(data))


_Base: Any = socketserver.ThreadingUnixStreamServer if UNIX_SOCKETS else object


class _Server(_Base):  # type: ignore[misc]
    daemon_threads = True
    behaviour: Behaviour
    release: threading.Event


@contextmanager
def fake_clamd() -> Iterator[tuple[str, Behaviour]]:
    """The socket's path (short: a unix socket's path is limited) and what the fake does."""
    directory = Path(tempfile.mkdtemp(prefix="clamd-"))
    address = str(directory / "c.sock")
    server = _Server(address, _Handler)
    server.behaviour = Behaviour()
    server.release = threading.Event()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield address, server.behaviour
    finally:
        server.release.set()
        server.shutdown()
        server.server_close()
        shutil.rmtree(directory, ignore_errors=True)


def refused_socket(directory: Path) -> str:
    """A socket file nothing listens on any more: what a stopped service leaves behind."""
    address = str(directory / "gone.sock")
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(address)
    return address
