"""Where attachment files live, and how their type is recognized (plan §5).

``ATTACHMENT_ROOT`` holds two folders on the private ``media`` volume:

- ``files/<storage_key>``: stored attachments, under a random name (64 hex characters).
  The backup copies the files its snapshot lists (``docker/backup/backup.sh``);
- ``staging/<reservation id>.upload`` and ``.out``: an upload being received and its
  processed output. Released reservations take theirs with them; the worker removes any
  left behind.

Nothing serves these folders: downloads go through ``views.download``.
"""

import hashlib
import secrets
from pathlib import Path
from uuid import UUID

from django.conf import settings

# Magic bytes of the accepted types (plan §5): the declared name and type are ignored.
JPEG, PNG, WEBP, PDF = "JPEG", "PNG", "WEBP", "PDF"
_SIGNATURES = (
    (JPEG, 0, b"\xff\xd8\xff"),
    (PNG, 0, b"\x89PNG\r\n\x1a\n"),
    (PDF, 0, b"%PDF-"),
)
_HEAD_BYTES = 16


def root() -> Path:
    return Path(settings.ATTACHMENT_ROOT)


def files_dir() -> Path:
    return root() / "files"


def staging_dir() -> Path:
    return root() / "staging"


def ensure_dirs() -> None:
    for folder in (files_dir(), staging_dir()):
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)


def new_storage_key() -> str:
    return secrets.token_hex(32)


def stored_path(storage_key: str) -> Path:
    if len(storage_key) != 64 or not all(c in "0123456789abcdef" for c in storage_key):
        raise ValueError("Not a storage key")
    return files_dir() / storage_key


def staged_input(reservation_id: UUID) -> Path:
    return staging_dir() / f"{reservation_id}.upload"


def staged_output(reservation_id: UUID) -> Path:
    return staging_dir() / f"{reservation_id}.out"


def discard(*paths: Path) -> None:
    """Delete these files if they exist."""
    for path in paths:
        path.unlink(missing_ok=True)


def discard_staged(reservation_id: UUID) -> None:
    discard(staged_input(reservation_id), staged_output(reservation_id))


def sniff(path: Path) -> str | None:
    """``JPEG``, ``PNG``, ``WEBP`` or ``PDF`` from the first bytes; ``None`` for anything
    else (an SVG, HTML or script named ``photo.jpg`` included)."""
    with path.open("rb") as handle:
        head = handle.read(_HEAD_BYTES)
    for kind, offset, signature in _SIGNATURES:
        if head[offset : offset + len(signature)] == signature:
            return kind
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return WEBP
    return None


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
