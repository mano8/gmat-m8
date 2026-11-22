"""What the settings services share: reading a choice as a member of its kind (M17.2)."""

from collections.abc import Mapping
from typing import Any

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.notifications.kinds import Channel, Mode, Notice

type NoticeKey = tuple[Notice, Channel]


def known[E: (Notice, Channel, Mode)](kind: type[E], value: str, field: str) -> E:
    """``value`` as a member of ``kind``, or a validation error on ``field``."""
    try:
        return kind(value)
    except ValueError:
        raise ValidationError(
            {field: ValidationError(_("Unknown choice."), code="invalid_choice")}
        ) from None


def flag(value: object, field: str) -> bool:
    """``value`` when it is a real boolean, otherwise a validation error on ``field``."""
    if isinstance(value, bool):
        return value
    raise ValidationError({field: ValidationError(_("Unknown choice."), code="invalid_choice")})


def known_key(key: tuple[Any, Any], field: str) -> NoticeKey:
    """A ``(notice, channel)`` key as members, or a validation error on ``field``."""
    try:
        notice, channel = key
    except TypeError, ValueError:
        raise ValidationError(
            {field: ValidationError(_("Unknown choice."), code="invalid_choice")}
        ) from None
    return known(Notice, notice, field), known(Channel, channel, field)


def flat(changes: Mapping[NoticeKey, Any]) -> dict[str, Any]:
    """``{"NOTICE/CHANNEL": value}``: a payload the idempotency hash can read, in a stable order."""
    return {
        f"{notice.value}/{channel.value}": value for (notice, channel), value in changes.items()
    }


def by_channel(changes: Mapping[NoticeKey, Any]) -> dict[str, dict[str, Any]]:
    """``{channel: {notice: value}}``: how an audit event reads a table of changes."""
    grouped: dict[str, dict[str, Any]] = {}
    for (notice, channel), value in changes.items():
        grouped.setdefault(channel.value, {})[notice.value] = value
    return grouped
