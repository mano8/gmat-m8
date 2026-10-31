"""Transaction helpers. Every locking write bounds its lock waits (CLAUDE.md, "Writes")."""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from django.core.exceptions import NON_FIELD_ERRORS, ValidationError
from django.db import DEFAULT_DB_ALIAS, IntegrityError, OperationalError, connections, transaction

DEFAULT_LOCK_TIMEOUT_MS = 5_000
_LOCK_NOT_AVAILABLE = "55P03"
_FOREIGN_KEY_VIOLATION = "23503"


class LockTimeout(Exception):
    """A row lock was not granted within the transaction's ``lock_timeout``."""


@contextmanager
def atomic_with_lock_timeout(
    lock_timeout_ms: int = DEFAULT_LOCK_TIMEOUT_MS, *, using: str = DEFAULT_DB_ALIAS
) -> Iterator[None]:
    """``transaction.atomic`` whose lock waits end after ``lock_timeout_ms``.

    ``SET LOCAL`` lasts until the outermost transaction ends, so inside an outer block the
    bound also applies to the rest of that block.
    """
    try:
        with transaction.atomic(using=using):
            with connections[using].cursor() as cursor:
                cursor.execute(
                    "SELECT set_config('lock_timeout', %s, true)", [f"{int(lock_timeout_ms)}ms"]
                )
            yield
    except OperationalError as exc:
        if getattr(exc.__cause__, "sqlstate", None) == _LOCK_NOT_AVAILABLE:
            raise LockTimeout(str(exc)) from exc
        raise


def db_now(*, using: str = DEFAULT_DB_ALIAS) -> datetime:
    """The database clock: the start time of the current transaction (plan §3.5, S8)."""
    with connections[using].cursor() as cursor:
        cursor.execute("SELECT now()")
        (now,) = cursor.fetchone()
    return now  # type: ignore[no-any-return]


def db_clock(*, using: str = DEFAULT_DB_ALIAS) -> datetime:
    """The database's wall clock right now (``clock_timestamp()``), not the transaction start.

    For a time that must order after the row locks the transaction waited for: an
    invitation created after an offboarding that committed while it waited is newer than
    that offboarding's ``offboarded_at``.
    """
    with connections[using].cursor() as cursor:
        cursor.execute("SELECT clock_timestamp()")
        (now,) = cursor.fetchone()
    return now  # type: ignore[no-any-return]


def _is_foreign_key_violation(exc: IntegrityError) -> bool:
    return getattr(exc.__cause__, "sqlstate", None) == _FOREIGN_KEY_VIOLATION


def constraint_name(exc: IntegrityError) -> str | None:
    """The name of the constraint or trigger-raised constraint behind an ``IntegrityError``."""
    diag = getattr(exc.__cause__, "diag", None)
    name = getattr(diag, "constraint_name", None)
    return name if isinstance(name, str) else None


@contextmanager
def constraint_errors(
    messages: Mapping[str, tuple[str | None, Any]],
    *,
    referenced: tuple[str | None, Any] | None = None,
    using: str = DEFAULT_DB_ALIAS,
) -> Iterator[None]:
    """Turn a violation of the named constraints into a field ``ValidationError``.

    ``messages`` maps a constraint name to ``(field or None, message)``. ``referenced`` is
    the answer to any foreign-key violation, which on a parent row means a child still
    points at the key being changed (the unit of a referenced material, C9). The write
    runs in a savepoint, so the service's transaction stays usable. This covers what only
    the database can settle: a concurrent insert of the same key, or a trigger's rule
    (``full_clean`` checks the same rules first, for the common case).
    """
    try:
        with transaction.atomic(using=using):
            yield
    except IntegrityError as exc:
        found = messages.get(constraint_name(exc) or "")
        if found is None and referenced is not None and _is_foreign_key_violation(exc):
            found = referenced
        if found is None:
            raise
        field, message = found
        raise ValidationError(
            {field or NON_FIELD_ERRORS: ValidationError(message, code="constraint")}
        ) from exc
