"""What ``recover.sh`` saved from the failed database before a restore (plan §9).

``docker_compose/gmat_m8/ops/failed-state.sql`` writes it with plain SQL, so a half-migrated
database can still answer: the terminal outcome of each outbox row and the access and
credential events of the audit log. ``post_restore_reconcile`` reads it to take those
outcomes instead of quarantining (F44) and to replay those events (F24, F52).
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from django.utils.dateparse import parse_datetime

FORMAT_VERSION = 1


class InvalidFailedState(ValueError):
    """The file is not what ``failed-state.sql`` writes: nothing may be replayed from it."""


@dataclass(frozen=True)
class AuditRow:
    id: UUID
    occurred_at: datetime
    actor_label: str
    action: str
    object_type: str
    object_id: UUID | None
    data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OutboxOutcome:
    id: UUID
    status: str
    sent_at: datetime | None


@dataclass(frozen=True)
class FailedState:
    audit: tuple[AuditRow, ...]
    outbox: tuple[OutboxOutcome, ...]


def _datetime(value: Any) -> datetime:
    parsed = parse_datetime(value) if isinstance(value, str) else None
    if parsed is None:
        raise ValueError(f"not a date and time: {value!r}")
    return parsed


def _audit_row(item: Mapping[str, Any]) -> AuditRow:
    return AuditRow(
        id=UUID(item["id"]),
        occurred_at=_datetime(item["occurred_at"]),
        actor_label=str(item["actor_label"]),
        action=str(item["action"]),
        object_type=str(item["object_type"]),
        object_id=UUID(item["object_id"]) if item["object_id"] else None,
        data=dict(item["data"] or {}),
    )


def _outcome(item: Mapping[str, Any]) -> OutboxOutcome:
    return OutboxOutcome(
        id=UUID(item["id"]),
        status=str(item["status"]),
        sent_at=_datetime(item["sent_at"]) if item["sent_at"] else None,
    )


def parse(raw: str) -> FailedState:
    """The parsed file; anything malformed raises ``InvalidFailedState``."""
    try:
        document = json.loads(raw)
        if document["format_version"] != FORMAT_VERSION:
            raise ValueError(f"format {document['format_version']!r}")
        return FailedState(
            audit=tuple(sorted(map(_audit_row, document["audit"]), key=lambda row: row.id)),
            outbox=tuple(map(_outcome, document["outbox"])),
        )
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise InvalidFailedState(f"not a failed-state file: {exc}") from exc
