"""Idempotent create commands (plan §4.8).

Every create form carries a key issued by the server (``issue_key``): a random ``key_id``
bound to the actor and the operation, signed with a ``TimestampSigner``. Inside the
service's transaction, right after ``authorize_in_txn``, ``idempotent()``:

1. rejects a key whose signature, actor or operation does not match (400), or that is
   older than ``IDEMPOTENCY_KEY_MAX_AGE`` (409, "this form has expired, please reload");
2. hashes the validated command (canonical JSON, decimals as strings);
3. inserts ``IdempotencyRecord(actor, key_id, operation, payload_hash)``:

   - new key: runs the command and stores a reference to its result;
   - same key and hash: a replay. ``replay`` must authorize again that the actor can still
     see the result, then return it (the view answers 303 to the document);
   - same key, other operation or hash: 409, nothing is written;
   - concurrent duplicate: the second insert waits on the unique index until the first
     transaction ends, then takes one of the two paths above (or runs as new if the first
     rolled back).

Records outlive the keys (7 days against 24 h), so purging can never turn a replay into a
new command: a key old enough to have lost its record fails its age check first.
"""

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from django.conf import settings
from django.core import signing
from django.db import DEFAULT_DB_ALIAS, IntegrityError, connections, transaction
from django.db.models import Model
from django.db.models.functions import Now

from apps.core.ids import new_id
from apps.core.models import IdempotencyRecord

SALT = "gmat.core.idempotency"
UNIQUE_CONSTRAINT = "core_idempotency_actor_key"


class IdempotencyError(Exception):
    """Base class; ``status_code`` is the HTTP status a view answers with."""

    status_code = 409


class IdempotencyKeyInvalid(IdempotencyError):
    """Tampered key, or a key issued to another actor or for another operation."""

    status_code = 400


class IdempotencyKeyExpired(IdempotencyError):
    """The key is older than ``IDEMPOTENCY_KEY_MAX_AGE``: the form must be reloaded."""


class IdempotencyConflict(IdempotencyError):
    """The key was already used for a different command."""


class IdempotencyOutsideTransaction(RuntimeError):
    """The record must commit or roll back together with the command it guards."""


@dataclass(frozen=True)
class IdempotentResult[T]:
    value: T
    replayed: bool


def _signer() -> signing.TimestampSigner:
    return signing.TimestampSigner(salt=SALT)


def issue_key(actor_id: UUID, operation: str) -> str:
    """Return a new signed key for one form of ``operation`` shown to ``actor_id``."""
    return _signer().sign_object({"k": str(new_id()), "a": str(actor_id), "o": operation})


def verify_key(key: str, *, actor_id: UUID, operation: str) -> UUID:
    """Return the key's ``key_id``; raise if the key is invalid or expired."""
    try:
        data = _signer().unsign_object(key, max_age=timedelta(seconds=_key_max_age()))
    except signing.SignatureExpired as exc:
        raise IdempotencyKeyExpired("This form has expired; please reload it") from exc
    except signing.BadSignature as exc:
        raise IdempotencyKeyInvalid("Invalid form key") from exc
    if not isinstance(data, dict) or data.get("a") != str(actor_id) or data.get("o") != operation:
        raise IdempotencyKeyInvalid("This form key belongs to another user or operation")
    try:
        return UUID(str(data.get("k")))
    except ValueError as exc:
        raise IdempotencyKeyInvalid("Invalid form key") from exc


def _key_max_age() -> int:
    return int(settings.IDEMPOTENCY_KEY_MAX_AGE)


def _canonical(value: Any) -> Any:
    if value is None or isinstance(value, bool | str):
        return value
    if isinstance(value, Enum):
        return _canonical(value.value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        raise TypeError("Floats are not allowed in a command payload; use Decimal")
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("Non-finite decimals are not allowed in a command payload")
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("Command payload keys must be strings")
        return {key: _canonical(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_canonical(item) for item in value]
    raise TypeError(f"Unsupported type in a command payload: {type(value).__name__}")


def payload_hash(payload: Mapping[str, Any]) -> str:
    """SHA-256 of the canonical JSON of a validated command (sorted keys, decimals as text)."""
    text = json.dumps(
        _canonical(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _is_duplicate_key(exc: IntegrityError) -> bool:
    diag = getattr(exc.__cause__, "diag", None)
    return getattr(diag, "constraint_name", None) == UNIQUE_CONSTRAINT


def idempotent[M: Model](
    *,
    actor_id: UUID,
    key: str,
    operation: str,
    payload: Mapping[str, Any],
    execute: Callable[[], M],
    replay: Callable[[str, UUID], M],
    using: str = DEFAULT_DB_ALIAS,
) -> IdempotentResult[M]:
    """Run ``execute`` once per key; on a replay return ``replay(result_type, result_id)``.

    ``execute`` returns the created object (its label and primary key are recorded).
    ``replay`` reloads that object for the actor, re-authorizing the read (S7: 404 out of
    scope, 403 without the capability). Call this inside the service's transaction.
    """
    if not connections[using].in_atomic_block:
        raise IdempotencyOutsideTransaction("idempotent() must run inside the service transaction")
    key_id = verify_key(key, actor_id=actor_id, operation=operation)
    digest = payload_hash(payload)
    try:
        # Savepoint: a duplicate key leaves the service's transaction usable.
        with transaction.atomic(using=using):
            record = IdempotencyRecord.objects.using(using).create(
                actor_id=actor_id, key_id=key_id, operation=operation, payload_hash=digest
            )
    except IntegrityError as exc:
        if not _is_duplicate_key(exc):
            raise
        existing = IdempotencyRecord.objects.using(using).get(actor_id=actor_id, key_id=key_id)
        if (
            existing.operation != operation
            or existing.payload_hash != digest
            or existing.result_id is None
        ):
            raise IdempotencyConflict("This form was already submitted with other data") from exc
        return IdempotentResult(replay(existing.result_type, existing.result_id), replayed=True)
    result = execute()
    record.result_type = result._meta.label_lower
    record.result_id = result.pk
    record.save(using=using, update_fields=["result_type", "result_id", "updated_at"])
    return IdempotentResult(result, replayed=False)


def purge_expired_records(*, using: str = DEFAULT_DB_ALIAS) -> int:
    """Delete records older than ``IDEMPOTENCY_RECORD_RETENTION`` (database clock)."""
    cutoff = Now() - timedelta(seconds=int(settings.IDEMPOTENCY_RECORD_RETENTION))
    deleted, _ = IdempotencyRecord.objects.using(using).filter(created_at__lt=cutoff).delete()
    return deleted
