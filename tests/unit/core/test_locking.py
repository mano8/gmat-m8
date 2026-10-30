"""Lock protocol helpers (apps.core.locking, plan §4.7).

Real deadlocks between connections are in tests/integration/test_deadlock_retry.py.
"""

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from django.db import DatabaseError, OperationalError, connection, transaction

from apps.core import locking
from apps.core.db import LockTimeout
from apps.core.locking import (
    LockLevel,
    LockMode,
    LockOrder,
    LockOrderViolation,
    LockOutsideTransaction,
    LockSetChanged,
    RetryExhausted,
    advisory_xact_lock,
    is_retryable,
    lock_discovered,
    run_in_transaction,
    transactional,
    try_advisory_xact_lock,
)
from apps.core.models import DocumentSequence


class SqlState(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


def db_error(sqlstate: str) -> OperationalError:
    error = OperationalError(f"sqlstate {sqlstate}")
    error.__cause__ = SqlState(sqlstate)
    return error


def sequences(*scopes: str) -> list[DocumentSequence]:
    return [DocumentSequence.objects.create(scope=scope) for scope in scopes]


def row_lock_modes(pk: uuid.UUID) -> set[str]:
    """The row-lock modes the current transaction holds on a ``core_documentsequence`` row."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT modes FROM pgrowlocks('core_documentsequence') WHERE locked_row = "
            "(SELECT ctid FROM core_documentsequence WHERE id = %s)",
            [pk],
        )
        row = cursor.fetchone()
    return set(row[0]) if row else set()


class TestLockLevel:
    def test_canonical_order(self) -> None:
        order = [
            LockLevel.ACCESS,
            LockLevel.IDEMPOTENCY,
            LockLevel.REQUEST_HEADERS,
            LockLevel.REQUEST_LINES,
            LockLevel.PO_HEADERS,
            LockLevel.PO_OUTBOX,
            LockLevel.PO_LINES,
            LockLevel.RECEIPTS,
            LockLevel.HANDOVERS,
            LockLevel.STORAGE,
            LockLevel.DOCUMENT_SEQUENCE,
        ]
        assert order == sorted(order)
        assert [level.name for level in order] == [
            "L0", "L1", "L2", "L3", "L4", "L4B", "L5", "L6", "L7", "L8", "L9",
        ]  # fmt: skip


@pytest.mark.django_db
class TestLockOrder:
    def test_locks_existing_rows_in_key_order(self) -> None:
        first, second = sequences("A", "B")
        missing = uuid.uuid7()
        with transaction.atomic():
            locked = LockOrder().lock(
                LockLevel.L9, DocumentSequence, [second.pk, missing, first.pk, second.pk]
            )
        assert locked == sorted([first.pk, second.pk])

    def test_nothing_to_lock(self) -> None:
        with transaction.atomic():
            assert LockOrder().lock(LockLevel.L2, DocumentSequence, []) == []

    def test_same_or_higher_level_is_allowed(self) -> None:
        locks = LockOrder()
        with transaction.atomic():
            locks.enter(LockLevel.L2)
            locks.enter(LockLevel.L2)
            locks.enter(LockLevel.L4B)
            locks.enter(LockLevel.L9)
        assert locks.level is LockLevel.L9

    def test_going_back_is_refused(self) -> None:
        locks = LockOrder()
        with transaction.atomic():
            locks.lock(LockLevel.L5, DocumentSequence, [])
            with pytest.raises(LockOrderViolation, match="L4 requested after L5"):
                locks.lock(LockLevel.L4, DocumentSequence, [])

    @pytest.mark.parametrize(
        ("mode", "expected"),
        [
            (LockMode.UPDATE, "For Update"),
            (LockMode.NO_KEY_UPDATE, "For No Key Update"),
            (LockMode.SHARE, "For Share"),
            (LockMode.KEY_SHARE, "For Key Share"),
        ],
    )
    def test_lock_modes(self, mode: LockMode, expected: str) -> None:
        with connection.cursor() as cursor:
            cursor.execute("CREATE EXTENSION IF NOT EXISTS pgrowlocks")
        (row,) = sequences("M")
        with transaction.atomic():
            LockOrder().lock(LockLevel.L0, DocumentSequence, [row.pk], mode=mode)
            assert expected in row_lock_modes(row.pk)

    @pytest.mark.django_db(transaction=True)
    def test_autocommit_is_refused(self) -> None:
        with pytest.raises(LockOutsideTransaction):
            LockOrder().lock(LockLevel.L2, DocumentSequence, [uuid.uuid7()])


@pytest.mark.django_db
class TestLockDiscovered:
    def test_a_stable_set_is_locked_and_returned(self) -> None:
        rows = sequences("D1", "D2")
        ids = [row.pk for row in rows]
        with transaction.atomic():
            locked = lock_discovered(LockOrder(), LockLevel.L3, DocumentSequence, lambda: ids)
        assert locked == sorted(ids)

    def test_a_set_that_grows_while_locking_is_retried(self) -> None:
        (row,) = sequences("D3")
        reads: list[list[uuid.UUID]] = [[row.pk], [row.pk, uuid.uuid7()]]
        with pytest.raises(LockSetChanged), transaction.atomic():
            lock_discovered(LockOrder(), LockLevel.L3, DocumentSequence, lambda: reads.pop(0))

    def test_a_row_deleted_before_the_lock_is_retried(self) -> None:
        ghost = uuid.uuid7()
        with pytest.raises(LockSetChanged), transaction.atomic():
            lock_discovered(LockOrder(), LockLevel.L3, DocumentSequence, lambda: [ghost])


class TestIsRetryable:
    @pytest.mark.parametrize("sqlstate", ["40P01", "40001", "55P03"])
    def test_conflicts_are_retryable(self, sqlstate: str) -> None:
        assert is_retryable(db_error(sqlstate))

    def test_lock_errors_are_retryable(self) -> None:
        assert is_retryable(LockTimeout("x"))
        assert is_retryable(LockSetChanged("x"))

    @pytest.mark.parametrize("exc", [db_error("23505"), DatabaseError("no cause"), ValueError("x")])
    def test_other_errors_are_not(self, exc: Exception) -> None:
        assert not is_retryable(exc)


@pytest.mark.django_db(transaction=True)
class TestRunInTransaction:
    def flaky(self, failures: list[Exception]) -> Callable[[], str]:
        def work() -> str:
            assert connection.in_atomic_block
            DocumentSequence.objects.create(scope=f"RUN:{uuid.uuid4()}")
            if failures:
                raise failures.pop(0)
            return "done"

        return work

    def test_success_commits(self) -> None:
        assert run_in_transaction(self.flaky([])) == "done"
        assert DocumentSequence.objects.count() == 1

    def test_conflicts_are_retried_with_jitter(self) -> None:
        delays: list[float] = []
        failures: list[Exception] = [db_error("40P01"), LockTimeout("x"), LockSetChanged("x")]
        result = run_in_transaction(self.flaky(failures), sleep=delays.append)
        assert result == "done"
        assert len(delays) == 3
        assert all(0 <= delay <= locking.RETRY_BASE_DELAY * 2**i for i, delay in enumerate(delays))
        # Every failed attempt rolled back; only the successful one committed.
        assert DocumentSequence.objects.count() == 1

    def test_gives_up_after_three_retries(self) -> None:
        failures: list[Exception] = [db_error("40P01") for _ in range(4)]
        with pytest.raises(RetryExhausted, match="please retry") as exc:
            run_in_transaction(self.flaky(failures), sleep=lambda delay: None)
        assert is_retryable(exc.value.__cause__)  # type: ignore[arg-type]
        assert failures == []
        assert not DocumentSequence.objects.exists()

    def test_other_errors_are_not_retried(self) -> None:
        failures: list[Exception] = [ValueError("invalid"), db_error("40P01")]
        with pytest.raises(ValueError, match="invalid"):
            run_in_transaction(self.flaky(failures), sleep=lambda delay: None)
        assert len(failures) == 1
        assert not DocumentSequence.objects.exists()

    def test_sets_the_lock_timeout(self) -> None:
        def show() -> Any:
            with connection.cursor() as cursor:
                cursor.execute("SHOW lock_timeout")
                return cursor.fetchone()[0]

        assert run_in_transaction(show, lock_timeout_ms=1500) in {"1500ms", "1500"}

    def test_inside_a_transaction_runs_once_without_retry(self) -> None:
        failures: list[Exception] = [db_error("40P01")]
        with transaction.atomic():
            with pytest.raises(OperationalError):
                run_in_transaction(self.flaky(failures), sleep=lambda delay: None)
            assert run_in_transaction(self.flaky([])) == "done"
        assert DocumentSequence.objects.count() == 1

    def test_decorator(self) -> None:
        @transactional
        def create(scope: str) -> bool:
            DocumentSequence.objects.create(scope=scope)
            return connection.in_atomic_block

        assert create("DECORATED") is True
        assert create.__name__ == "create"
        assert DocumentSequence.objects.filter(scope="DECORATED").exists()


@pytest.mark.django_db
class TestAdvisoryLocks:
    def test_the_media_backup_key_matches_the_backup_scripts(self) -> None:
        assert locking.MEDIA_BACKUP_LOCK == "gmat:media-backup"

    def test_take_and_try(self) -> None:
        with transaction.atomic():
            advisory_xact_lock("gmat:test")
            # The same session may take it again; another session could not.
            assert try_advisory_xact_lock("gmat:test") is True
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = "
                    "pg_backend_pid()"
                )
                assert cursor.fetchone()[0] >= 1

    @pytest.mark.django_db(transaction=True)
    def test_autocommit_is_refused(self) -> None:
        with pytest.raises(LockOutsideTransaction):
            advisory_xact_lock("gmat:test")
        with pytest.raises(LockOutsideTransaction):
            try_advisory_xact_lock("gmat:test")
