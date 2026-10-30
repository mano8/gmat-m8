"""``record()``: write an audit event inside the service's transaction (plan §4.7).

Every mutating service ends with ``record(...)`` and then ``events.publish(...)``, in the
same transaction as its write, so the trail and the change commit or roll back together.
The ``data`` diff or snapshot must hold no secrets, tokens or passwords.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from django.db import DEFAULT_DB_ALIAS, connections
from django.db.models import Model

from apps.audit.models import AuditEvent
from config.logging import request_id_var

ACTION = re.compile(r"[a-z][a-z_]*(\.[a-z][a-z_]*)+")


@dataclass(frozen=True)
class AuditIdentity:
    """Who acted, as the audit shows it (plan §3.6, "Audit identity").

    Principals (``apps.access.principals``) expose one: a user (``user_id`` + email), the worker
    (``system:worker``, no user) or an operator (``via="cli:<command>"``).
    """

    label: str
    user_id: UUID | None = None
    via: str = ""
    ip: str | None = None


class AuditOutsideTransaction(RuntimeError):
    """An audit row must commit or roll back with the change it describes."""


def record(
    identity: AuditIdentity,
    action: str,
    *,
    obj: Model | None = None,
    branch_id: UUID | None = None,
    data: Mapping[str, Any] | None = None,
    using: str = DEFAULT_DB_ALIAS,
) -> AuditEvent:
    """Append one audit event for ``action`` (``noun.verb``, e.g. ``request.submitted``)."""
    if not ACTION.fullmatch(action):
        raise ValueError(f"Invalid audit action {action!r}; expected 'noun.verb'")
    if not identity.label:
        raise ValueError("An audit identity needs a label")
    if not connections[using].in_atomic_block:
        raise AuditOutsideTransaction("record() must run inside the service transaction")
    return AuditEvent.objects.using(using).create(
        actor_id=identity.user_id,
        actor_label=identity.label,
        via=identity.via,
        action=action,
        object_type=obj._meta.label_lower if obj is not None else "",
        object_id=obj.pk if obj is not None else None,
        branch_id=branch_id,
        data=dict(data or {}),
        request_id=request_id_var.get() or "",
        ip=identity.ip,
    )
