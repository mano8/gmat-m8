"""Typed environment helpers that fail closed.

A missing required value, an unparsable value or a too-short required string raises
``ImproperlyConfigured`` at import time, so a misconfigured deployment never starts.
"""

import os
from collections.abc import Mapping

from django.core.exceptions import ImproperlyConfigured

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


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
