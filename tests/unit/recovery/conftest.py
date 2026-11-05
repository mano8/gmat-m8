"""A database just restored from a backup: an operator and a failed state to replay."""

import json
from collections.abc import Callable
from datetime import datetime
from typing import Any
from uuid import UUID

import pytest
from django.utils import timezone

from apps.access.principals import SessionPrincipal
from apps.accounts.models import User
from apps.core.ids import new_id
from apps.recovery import failed_state
from tests.org import People
from tests.support import principal_for


@pytest.fixture
def operator(people: People) -> User:
    """The platform operator doing the recovery, as ``bootstrap_admin --superuser`` makes one."""
    User.objects.filter(pk=people.group_admin.pk).update(is_superuser=True, is_staff=True)
    people.group_admin.refresh_from_db()
    return people.group_admin


@pytest.fixture
def principal(operator: User) -> SessionPrincipal:
    return principal_for(operator)


type Event = Callable[..., dict[str, Any]]


def audit_event(
    action: str, object_id: UUID | None, data: dict[str, Any] | None = None, **fields: Any
) -> dict[str, Any]:
    """One row as ``ops/failed-state.sql`` writes it; a new id is newer than the snapshot."""
    when: datetime = fields.pop("occurred_at", timezone.now())
    return {
        "id": str(fields.pop("id", new_id())),
        "occurred_at": when.isoformat(),
        "actor_label": "admin@example.org",
        "action": action,
        "object_type": fields.pop("object_type", "accounts.user"),
        "object_id": str(object_id) if object_id else None,
        "data": data or {},
    }


def state_of(
    *events: dict[str, Any], outbox: tuple[dict[str, Any], ...] = ()
) -> failed_state.FailedState:
    return failed_state.parse(
        json.dumps({"format_version": 1, "audit": list(events), "outbox": list(outbox)})
    )
