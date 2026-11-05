"""``web``'s side of the image decoder service (``decoder_service``; M8.3, ADR-0015).

What comes back is treated as untrusted: its size is bounded here, and ``processing`` checks it
is a WebP file before it can be stored.
"""

import json
import socket
import time
from pathlib import Path

from django.conf import settings

from apps.attachments.decoder import EXIT_TOO_LARGE
from apps.attachments.decoder_service import CHUNK, EXIT_OK, EXIT_TIMEOUT
from apps.attachments.decoder_service import ping as service_ping

# The service kills the child at the deadline and answers: a little more than that here.
MARGIN = 15


class DecoderUnavailable(Exception):
    """Nothing listens on the socket, or the connection broke before an answer."""


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


def _save(connection: socket.socket, target: Path, deadline: float) -> int:
    """Write what the service sends to ``target``, no more than a stored file may be."""
    limit = settings.ATTACHMENT_MAX_FILE_BYTES
    written = 0
    with target.open("wb") as stream:
        while True:
            connection.settimeout(_left(deadline))
            data = connection.recv(CHUNK)
            if not data:
                return EXIT_OK
            written += len(data)
            if written > limit:
                return EXIT_TOO_LARGE
            stream.write(data)


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
            connection.settimeout(_left(deadline))
            status = connection.recv(1)
            if not status:
                raise DecoderUnavailable("No answer")
            if status[0] != EXIT_OK:
                return status[0]
            return _save(connection, target, deadline)
    except TimeoutError:
        return EXIT_TIMEOUT
    except OSError as exc:
        raise DecoderUnavailable(type(exc).__name__) from exc


def ping() -> bool:
    """Whether the service answers (``manage verify_internal``); nothing to ask without it."""
    address = settings.ATTACHMENT_DECODER_SOCKET
    return not address or service_ping(address)
