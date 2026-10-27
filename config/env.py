"""Typed environment helpers that fail closed.

A missing required value, an unparsable value or a too-short required string raises
``ImproperlyConfigured`` at import time, so a misconfigured deployment never starts.
"""

import base64
import hashlib
import os
from collections.abc import Mapping

from cryptography.fernet import Fernet
from django.core.exceptions import ImproperlyConfigured

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})

# Values copied from templates or examples, never real secrets.
_PLACEHOLDER_PREFIXES = ("dev-", "test-", "build-", "change", "insecure", "placeholder", "example")
_PLACEHOLDER_MARKERS = ("change-me", "changeme", "replace-me", "your-", "xxxxxxxx")


def is_placeholder(value: str) -> bool:
    """True for template, example or development values that must never reach production."""
    lowered = value.strip().lower()
    return lowered.startswith(_PLACEHOLDER_PREFIXES) or any(
        marker in lowered for marker in _PLACEHOLDER_MARKERS
    )


def derived_fernet_key(label: str) -> str:
    """A deterministic Fernet key for development and tests only (never a production key).

    Deriving it keeps key material out of the repository; production refuses these values.
    """
    digest = hashlib.sha256(f"gmat-m8 non-production key: {label}".encode()).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii")


def _raw(name: str, environ: Mapping[str, str] | None) -> str | None:
    value = (os.environ if environ is None else environ).get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def env_str(
    name: str,
    default: str | None = None,
    *,
    min_length: int = 1,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Return a string; without a default the variable is required."""
    value = _raw(name, environ)
    if value is None:
        if default is None:
            raise ImproperlyConfigured(f"Environment variable {name} is required")
        value = default
    if len(value) < min_length:
        raise ImproperlyConfigured(
            f"Environment variable {name} must have at least {min_length} characters"
        )
    return value


def env_bool(name: str, default: bool, *, environ: Mapping[str, str] | None = None) -> bool:
    """Return a boolean; anything but a known true/false word is an error."""
    value = _raw(name, environ)
    if value is None:
        return default
    lowered = value.lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ImproperlyConfigured(
        f"Environment variable {name} must be one of {sorted(_TRUE | _FALSE)}"
    )


def env_int(
    name: str,
    default: int | None = None,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Return an integer, optionally bounded; without a default the variable is required."""
    value = _raw(name, environ)
    if value is None:
        if default is None:
            raise ImproperlyConfigured(f"Environment variable {name} is required")
        return default
    try:
        number = int(value)
    except ValueError:
        raise ImproperlyConfigured(f"Environment variable {name} must be an integer") from None
    if (minimum is not None and number < minimum) or (maximum is not None and number > maximum):
        raise ImproperlyConfigured(f"Environment variable {name} is out of range")
    return number


def env_list(
    name: str,
    default: list[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    """Return a comma-separated list without blanks; without a default it is required."""
    value = _raw(name, environ)
    if value is None:
        if default is None:
            raise ImproperlyConfigured(f"Environment variable {name} is required")
        return list(default)
    items = [item.strip() for item in value.split(",") if item.strip()]
    if not items:
        raise ImproperlyConfigured(f"Environment variable {name} must not be empty")
    return items


def env_secret(name: str, *, min_length: int, environ: Mapping[str, str] | None = None) -> str:
    """Return a required secret; a short or placeholder value is an error."""
    value = env_str(name, min_length=min_length, environ=environ)
    if is_placeholder(value):
        raise ImproperlyConfigured(f"Environment variable {name} is a placeholder")
    return value


def fernet_key_problems(keys: list[str], *, refused: frozenset[str] = frozenset()) -> list[str]:
    """Describe why a Fernet key list is unusable; an empty list means it is valid."""
    if not keys:
        return ["no key is configured"]
    problems: list[str] = []
    if len(set(keys)) != len(keys):
        problems.append("a key is listed twice")
    for position, key in enumerate(keys, start=1):
        if key in refused or is_placeholder(key):
            problems.append(f"key {position} is a development or placeholder key")
            continue
        try:
            Fernet(key.encode("ascii"))
        except ValueError, UnicodeEncodeError:
            problems.append(f"key {position} is not a valid Fernet key")
    return problems


def env_fernet_keys(
    name: str,
    *,
    refused: frozenset[str] = frozenset(),
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    """Return a required, comma-separated list of Fernet keys, newest first."""
    keys = env_list(name, environ=environ)
    problems = fernet_key_problems(keys, refused=refused)
    if problems:
        raise ImproperlyConfigured(f"Environment variable {name}: {'; '.join(problems)}")
    return keys
