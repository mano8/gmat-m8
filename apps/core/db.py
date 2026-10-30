"""Transaction helpers. Every locking write bounds its lock waits (CLAUDE.md, "Writes")."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

from django.db import DEFAULT_DB_ALIAS, OperationalError, connections, transaction

DEFAULT_LOCK_TIMEOUT_MS = 5_000
_LOCK_NOT_AVAILABLE = "55P03"


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
