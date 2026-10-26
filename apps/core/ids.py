"""Identifier helpers. Primary keys are UUIDv7: time-ordered, safe to expose in URLs."""

import uuid


def new_id() -> uuid.UUID:
    """Return a new UUIDv7 (Python 3.14 standard library)."""
    return uuid.uuid7()
