"""Lock protocol for mutating services (plan §4.7).

- ``LockLevel``: the canonical order L0-L9 (with L4b). A service skips the levels it does
  not need but never goes backwards; ``LockOrder`` enforces that inside one transaction.
- ``lock_discovered``: discover → lock → verify for rows found by reading other rows.
- ``run_in_transaction`` / ``@transactional``: the one retry wrapper. It runs the work in
  ``transaction.atomic()`` with ``lock_timeout`` and retries deadlocks, lock timeouts,
  serialization failures and changed lock sets up to 3 times with jitter.
- ``advisory_xact_lock``: named advisory locks outside the row order (``gmat:media-backup``).

Material rows are never locked (C9 is held by the composite foreign key).
"""

import functools
import random
import time
from collections.abc import Callable, Iterable
from enum import Enum, IntEnum
from uuid import UUID

from django.db import DEFAULT_DB_ALIAS, DatabaseError, connections
from django.db.models import Model

from apps.core.db import DEFAULT_LOCK_TIMEOUT_MS, LockTimeout, atomic_with_lock_timeout

MAX_RETRIES = 3
RETRY_BASE_DELAY = 0.05  # seconds; attempt n waits up to base * 2**n
MEDIA_BACKUP_LOCK = "gmat:media-backup"  # same key as docker/backup/common.sh

_DEADLOCK = "40P01"
_SERIALIZATION_FAILURE = "40001"
_LOCK_NOT_AVAILABLE = "55P03"
RETRYABLE_SQLSTATES = frozenset({_DEADLOCK, _SERIALIZATION_FAILURE, _LOCK_NOT_AVAILABLE})


class LockLevel(IntEnum):
    """Canonical lock order (plan §4.7). Each level has a number name and a row name."""

    L0 = 0  # AccessState, then the principal's User row (FOR SHARE; changes FOR UPDATE)
    L1 = 10  # IdempotencyRecord insert (unique index)
    L2 = 20  # material request headers
    L3 = 30  # material request lines
    L4 = 40  # purchase-order headers
    L4B = 45  # outbox rows of those orders (dispatches)
    L5 = 50  # purchase-order lines
    L6 = 60  # goods-receipt headers
    L7 = 70  # handover headers
    L8 = 80  # StorageQuota, then attachment reservations
    L9 = 90  # DocumentSequence

    ACCESS = 0
    IDEMPOTENCY = 10
    REQUEST_HEADERS = 20
    REQUEST_LINES = 30
    PO_HEADERS = 40
    PO_OUTBOX = 45
    PO_LINES = 50
    RECEIPTS = 60
    HANDOVERS = 70
    STORAGE = 80
    DOCUMENT_SEQUENCE = 90


class LockMode(Enum):
    UPDATE = "UPDATE"
    NO_KEY_UPDATE = "NO KEY UPDATE"
    SHARE = "SHARE"
    KEY_SHARE = "KEY SHARE"


class LockOrderViolation(RuntimeError):
    """A lock was requested at a level below one already held: a programming error."""


class LockOutsideTransaction(RuntimeError):
    """Row and transaction-level advisory locks are released at once in autocommit mode."""


class LockSetChanged(Exception):
    """Discover → lock → verify found a different set under the locks; retry the transaction."""


class RetryExhausted(Exception):
    """Still conflicting after every retry; the user is asked to try again."""


def _require_transaction(using: str) -> None:
    if not connections[using].in_atomic_block:
        raise LockOutsideTransaction("Locks must be taken inside the service transaction")


class LockOrder:
    """Takes row locks in canonical order within one transaction.

    Create one per service call and take every row lock through it. Rows of one level are
    locked in primary-key order. Locking again at the level already reached is allowed;
    going back to a lower level raises ``LockOrderViolation``.
    """

    def __init__(self, using: str = DEFAULT_DB_ALIAS) -> None:
        self.using = using
        self.level: LockLevel | None = None

    def enter(self, level: LockLevel) -> None:
        """Record that locks of ``level`` are being taken (also for locks taken elsewhere)."""
        if self.level is not None and level < self.level:
            raise LockOrderViolation(
                f"Lock at {level.name} requested after {self.level.name} (plan §4.7)"
            )
        self.level = level

    def lock(
        self,
        level: LockLevel,
        model: type[Model],
        ids: Iterable[UUID],
        *,
        mode: LockMode = LockMode.UPDATE,
    ) -> list[UUID]:
        """Lock the rows of ``model`` with these primary keys; return the ids that exist."""
        _require_transaction(self.using)
        self.enter(level)
        wanted = sorted(set(ids))
        if not wanted:
            return []
        connection = connections[self.using]
        table = connection.ops.quote_name(model._meta.db_table)
        pk = connection.ops.quote_name(str(model._meta.pk.column))
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT {pk} FROM {table} WHERE {pk} = ANY(%s)"  # noqa: S608  # nosec B608
                f" ORDER BY {pk} FOR {mode.value}",
                [wanted],
            )
            return [row[0] for row in cursor.fetchall()]


def lock_discovered(
    locks: LockOrder,
    level: LockLevel,
    model: type[Model],
    discover: Callable[[], Iterable[UUID]],
    *,
    mode: LockMode = LockMode.UPDATE,
) -> list[UUID]:
    """Discover → lock → verify (plan §4.7); return the locked ids in order.

    Reads the candidate set without locks, locks it, then reads it again under the locks.
    If the set changed in between, raises ``LockSetChanged``; the retry wrapper rolls the
    transaction back and runs the service again.
    """
    candidates = set(discover())
    locked = set(locks.lock(level, model, candidates, mode=mode))
    if locked != candidates or set(discover()) != candidates:
        raise LockSetChanged(f"The set of {model._meta.label} rows changed while locking")
    return sorted(candidates)


def is_retryable(exc: BaseException) -> bool:
    """Deadlock, lock timeout, serialization failure or a changed lock set."""
    if isinstance(exc, LockTimeout | LockSetChanged):
        return True
    if isinstance(exc, DatabaseError):
        return getattr(exc.__cause__, "sqlstate", None) in RETRYABLE_SQLSTATES
    return False


def _backoff(attempt: int) -> float:
    return random.uniform(0, RETRY_BASE_DELAY * 2**attempt)  # noqa: S311  # nosec B311 - jitter


def run_in_transaction[T](
    work: Callable[[], T],
    *,
    max_retries: int = MAX_RETRIES,
    lock_timeout_ms: int = DEFAULT_LOCK_TIMEOUT_MS,
    using: str = DEFAULT_DB_ALIAS,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Run ``work`` in one transaction with ``lock_timeout``; retry conflicts.

    Deadlocks, lock timeouts, serialization failures and ``LockSetChanged`` roll the
    transaction back and run ``work`` again, up to ``max_retries`` times with jitter, then
    raise ``RetryExhausted``. Nothing is half-written: every attempt rolls back entirely.

    Inside an enclosing transaction there is nothing to retry (the enclosing one holds its
    locks): ``work`` runs once in a savepoint and errors propagate to the owner of the
    outer transaction.
    """
    if connections[using].in_atomic_block:
        with atomic_with_lock_timeout(lock_timeout_ms, using=using):
            return work()
    attempt = 0
    while True:
        try:
            with atomic_with_lock_timeout(lock_timeout_ms, using=using):
                return work()
        except Exception as exc:
            if not is_retryable(exc):
                raise
            if attempt >= max_retries:
                raise RetryExhausted("The operation conflicted with others; please retry") from exc
            sleep(_backoff(attempt))
            attempt += 1


def transactional[**P, T](func: Callable[P, T]) -> Callable[P, T]:
    """Decorator: run the function through ``run_in_transaction`` with the defaults."""

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        return run_in_transaction(lambda: func(*args, **kwargs))

    return wrapper


def advisory_xact_lock(name: str, *, using: str = DEFAULT_DB_ALIAS) -> None:
    """Wait for the named advisory lock; it is released when the transaction ends."""
    _require_transaction(using)
    with connections[using].cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [name])


def try_advisory_xact_lock(name: str, *, using: str = DEFAULT_DB_ALIAS) -> bool:
    """Take the named advisory lock if it is free; held until the transaction ends."""
    _require_transaction(using)
    with connections[using].cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))", [name])
        (acquired,) = cursor.fetchone()
    return bool(acquired)
