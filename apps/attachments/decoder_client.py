"""``web``'s side of the image decoder service (``decoder_service``; M8.3, ADR-0015).

What comes back is treated as untrusted: its size is bounded and read exactly, and ``processing``
checks it is a WebP file before it can be stored. ``decoder_service`` needs unix sockets at import
time, so this module speaks its protocol without importing it, and touches ``socket.AF_UNIX`` only
when a decoder is configured (production; never a Windows development machine).
"""

import json
import socket
import struct
import time
from pathlib import Path

from django.conf import settings

from apps.attachments.decoder import EXIT_OK, EXIT_TIMEOUT, EXIT_TOO_LARGE

# The service kills the child at the deadline and answers: a little more than that here.
MARGIN = 15
CHUNK = 64 * 1024
SIZE_FORMAT = "!Q"
PING_TIMEOUT = 3.0


class DecoderUnavailable(Exception):
    """Nothing listens on the socket, or the connection broke before a whole answer."""


def _left(deadline: float) -> float:
    return max(deadline - time.monotonic(), 0.001)


def _request(source: Path, image_format: str) -> bytes:
    header = {
        "format": image_format,
        "max_pixels": settings.ATTACHMENT_MAX_PIXELS,
        "max_side": settings.ATTACHMENT_MAX_SIDE,
        "max_bytes": settings.ATTACHMENT_MAX_FILE_BYTES,
        "memory": settings.ATTACHMENT_DECODE_MEMORY,
        "cpu": settings.ATTACHMENT_DECODE_CPU,
        "timeout": settings.ATTACHMENT_DECODE_TIMEOUT,
        "size": source.stat().st_size,
    }
    return json.dumps(header).encode("ascii") + b"\n"


def _exactly(connection: socket.socket, size: int, deadline: float) -> bytes:
    """``size`` bytes at most ``CHUNK``, or the answer was cut short."""
    received = b""
    while len(received) < size:
        connection.settimeout(_left(deadline))
        data = connection.recv(size - len(received))
        if not data:
            raise DecoderUnavailable("The answer was cut short")
        received += data
    return received


def _save(connection: socket.socket, target: Path, deadline: float) -> int:
    """Write the announced file to ``target``, if it is no more than a stored file may be."""
    size_bytes = _exactly(connection, struct.calcsize(SIZE_FORMAT), deadline)
    (left,) = struct.unpack(SIZE_FORMAT, size_bytes)
    if left > settings.ATTACHMENT_MAX_FILE_BYTES:
        return EXIT_TOO_LARGE
    with target.open("wb") as stream:
        while left:
            stream.write(data := _exactly(connection, min(CHUNK, left), deadline))
            left -= len(data)
    return EXIT_OK


def decode(source: Path, target: Path, image_format: str) -> int:
    """The decoder's exit status, ``target`` holding its output when that is ``EXIT_OK``."""
    deadline = time.monotonic() + settings.ATTACHMENT_DECODE_TIMEOUT + MARGIN
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(_left(deadline))
            connection.connect(settings.ATTACHMENT_DECODER_SOCKET)
            connection.sendall(_request(source, image_format))
            with source.open("rb") as stream:
                connection.sendfile(stream)
            (status,) = _exactly(connection, 1, deadline)
            return _save(connection, target, deadline) if status == EXIT_OK else status
    except TimeoutError:
        return EXIT_TIMEOUT
    except OSError as exc:
        raise DecoderUnavailable(type(exc).__name__) from exc


def ping() -> bool:
    """Whether the service answers (``manage verify_internal``); nothing to ask without it."""
    address = settings.ATTACHMENT_DECODER_SOCKET
    if not address:
        return True
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(PING_TIMEOUT)
            connection.connect(address)
            connection.sendall(b'{"ping": true}\n')
            return connection.recv(1) == bytes([EXIT_OK])
    except OSError:
        return False
