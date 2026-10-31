"""Gapless document numbers (plan §4.2, §4.7 L9).

``next_number`` is called last in a service's transaction, after every other lock. It
takes the scope's row with one ``INSERT … ON CONFLICT DO UPDATE``, which creates the row on
first use and otherwise increments it under a row lock held until the transaction ends.
Concurrent callers queue on that lock; a rollback undoes the increment, so numbers have no
gaps and never repeat.
"""

from django.db import DEFAULT_DB_ALIAS, connections
from django.utils import timezone

from apps.core.ids import new_id
from apps.core.models import DocumentSequence

SCOPE_MAX_LENGTH = 100


class SequenceOutsideTransaction(RuntimeError):
    """A number taken in autocommit mode could not be returned on failure."""


def next_number(scope: str, *, using: str = DEFAULT_DB_ALIAS) -> int:
    """Return the next number of ``scope`` (1, 2, 3, …), reserved by the current transaction."""
    if not scope or len(scope) > SCOPE_MAX_LENGTH:
        raise ValueError(f"A sequence scope is 1 to {SCOPE_MAX_LENGTH} characters")
    connection = connections[using]
    if not connection.in_atomic_block:
        raise SequenceOutsideTransaction("next_number() must run inside the document's transaction")
    table = connection.ops.quote_name(DocumentSequence._meta.db_table)
    now = timezone.now()
    with connection.cursor() as cursor:
        cursor.execute(
            f"INSERT INTO {table} (id, created_at, updated_at, scope, next_value)"  # noqa: S608  # nosec B608
            " VALUES (%s, %s, %s, %s, 2)"
            " ON CONFLICT (scope) DO UPDATE"
            f" SET next_value = {table}.next_value + 1, updated_at = EXCLUDED.updated_at"
            " RETURNING next_value - 1",
            [new_id(), now, now, scope],
        )
        (value,) = cursor.fetchone()  # RETURNING always yields the row
    return int(value)


def format_number(prefix: str, owner_code: str, year: int, value: int, *, width: int = 5) -> str:
    """``SOL-{branch}-{yyyy}-{00001}`` and friends (plan §4.2)."""
    return f"{prefix}-{owner_code}-{year:04d}-{value:0{width}d}"
