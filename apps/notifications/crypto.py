"""Sealing of sensitive outbox bodies with ``OUTBOX_ENCRYPTION_KEYS`` (plan §5).

MultiFernet, newest key first: the first key encrypts, every listed key decrypts. The keys
are separate from ``SECRET_KEY`` and from ``MFA_ENCRYPTION_KEYS`` (check ``gmat.E018``). A
sealed body lives at most an hour (``OUTBOX_SENSITIVE_TTL``) before it is wiped, so a
rotation only has to keep the old key listed for that long.
"""

from functools import lru_cache

from cryptography.fernet import Fernet, MultiFernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


@lru_cache(maxsize=4)
def _multi_fernet(keys: tuple[str, ...]) -> MultiFernet:
    if not keys:
        raise ImproperlyConfigured("OUTBOX_ENCRYPTION_KEYS is empty")
    return MultiFernet([Fernet(key.encode("ascii")) for key in keys])


def _fernet() -> MultiFernet:
    return _multi_fernet(tuple(settings.OUTBOX_ENCRYPTION_KEYS))


def seal(text: str) -> str:
    """Encrypt ``text``; the empty string stays empty (nothing to protect)."""
    if not text:
        return ""
    return _fernet().encrypt(text.encode("utf-8")).decode("ascii")


def unseal(token: str) -> str:
    """Decrypt a sealed body; raises ``cryptography.fernet.InvalidToken`` for a foreign key."""
    if not token:
        return ""
    return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
