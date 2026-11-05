"""Phase 2 of an upload: recognize, check and convert the received file (plan §5, F54).

It runs outside any transaction, after admission and before finalization:

- the type comes from the magic bytes only (``files.sniff``): JPEG, PNG, WebP or PDF;
- every file is scanned for malware, and none is stored without a clean verdict (``scanner``;
  M8.3, ADR-0015);
- an image is decoded and re-encoded to WebP in a **child process** (``decoder``) with
  resource limits (``ATTACHMENT_DECODE_MEMORY`` address space, ``ATTACHMENT_DECODE_CPU``
  seconds of CPU) and a **hard kill** after ``ATTACHMENT_DECODE_TIMEOUT`` seconds of wall
  time, so a stuck or hostile decoder never outlives the reservation's lease. In production
  that child runs in a container of its own, under its own uid (``decoder_service``), and what
  comes back must be a WebP file;
- a PDF is stored as it arrived and never rendered on the server.

``Rejected`` carries a code the view turns into an answer; the caller releases the
reservation.
"""

import logging
import subprocess  # nosec B404 - a fixed command: this interpreter and the decoder script
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from django.conf import settings
from django.utils.translation import gettext_lazy as _

from apps.accounts.security_log import log_event
from apps.attachments import decoder, decoder_client, files, scanner

if TYPE_CHECKING:
    from django.utils.functional import _StrPromise as Message


logger = logging.getLogger(__name__)

DECODER = Path(decoder.__file__).resolve()
WEBP, PDF = "image/webp", "application/pdf"


@dataclass(frozen=True)
class Processed:
    """The file to store: where it is, its type, size and digest."""

    path: Path
    content_type: str
    size: int
    sha256: str


class Rejected(Exception):
    """The file cannot be stored; ``code`` names why (``REJECTIONS``); ``signature`` what the
    scanner found, for the audit trail."""

    def __init__(self, code: str, signature: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.signature = signature

    @property
    def message(self) -> Message:
        return REJECTIONS[self.code]


REJECTIONS: dict[str, Message] = {
    "type": _("Only JPEG, PNG and WebP images or PDF files can be attached."),
    "too_many_pixels": _("This image has too many pixels."),
    "too_large": _("The file is too large."),
    "undecodable": _("This image could not be read."),
    "timeout": _("This image took too long to process."),
    "empty": _("The file is empty."),
    "malware": _("This file was refused: the virus scanner found something in it."),
    "unavailable": _("Files cannot be checked right now. Try again in a few minutes."),
}

_EXIT_CODES = {
    decoder.EXIT_FORMAT: "type",
    decoder.EXIT_TOO_MANY_PIXELS: "too_many_pixels",
    decoder.EXIT_TOO_LARGE: "too_large",
    decoder.EXIT_TIMEOUT: "timeout",
}


def decoder_command(source: Path, target: Path, image_format: str) -> list[str]:
    """The child's command line (isolated mode: no current directory, no ``PYTHON*``)."""
    limits = (
        settings.ATTACHMENT_MAX_PIXELS,
        settings.ATTACHMENT_MAX_SIDE,
        settings.ATTACHMENT_MAX_FILE_BYTES,
        settings.ATTACHMENT_DECODE_MEMORY,
        settings.ATTACHMENT_DECODE_CPU,
    )
    return [
        sys.executable,
        "-I",
        str(DECODER),
        str(source),
        str(target),
        image_format,
        *(str(value) for value in limits),
    ]


def _decode_in_child(source: Path, target: Path, image_format: str) -> int:
    """Run the decoder child; ``subprocess.run`` kills it at the deadline."""
    try:
        result = subprocess.run(  # noqa: S603  # nosec B603 - see decoder_command
            decoder_command(source, target, image_format),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=settings.ATTACHMENT_DECODE_TIMEOUT,
            check=False,
            env={"LANG": "C.UTF-8"},
        )
    except subprocess.TimeoutExpired:
        return decoder.EXIT_TIMEOUT
    return result.returncode


def _decode(source: Path, target: Path, image_format: str) -> None:
    """In the decoder's container (production) or a child of this process."""
    try:
        if settings.ATTACHMENT_DECODER_SOCKET:
            status = decoder_client.decode(source, target, image_format)
        else:
            status = _decode_in_child(source, target, image_format)
    except decoder_client.DecoderUnavailable as exc:
        raise Rejected("unavailable") from exc
    if status != decoder.EXIT_OK:
        raise Rejected(_EXIT_CODES.get(status, "undecodable"))


def _scan(source: Path) -> None:
    """No file goes further without a clean verdict."""
    try:
        scanner.scan(source)
    except scanner.Infected as exc:
        log_event("attachment_malware", None, level=logging.WARNING, signature=exc.signature)
        raise Rejected("malware", exc.signature) from exc
    except scanner.ScannerUnavailable as exc:
        logger.error("The malware scanner gave no verdict: %s", exc)
        raise Rejected("unavailable") from exc


def process(source: Path, target: Path) -> Processed:
    """Check ``source`` and write what is stored to ``target``."""
    kind = files.sniff(source)
    if kind is None:
        raise Rejected("type")
    _scan(source)
    if kind == files.PDF:
        source.replace(target)
        content_type = PDF
    else:
        _decode(source, target, kind)
        content_type = WEBP
    size = target.stat().st_size
    if size == 0:
        raise Rejected("empty")
    if size > settings.ATTACHMENT_MAX_FILE_BYTES:
        raise Rejected("too_large")
    if content_type == WEBP and files.sniff(target) != files.WEBP:
        raise Rejected("undecodable")
    return Processed(target, content_type, size, files.sha256_of(target))
