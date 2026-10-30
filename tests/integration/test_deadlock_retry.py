"""A forced deadlock is retried successfully by the shared wrapper (plan §4.7)."""

import threading
from collections.abc import Callable

import pytest

from apps.core.locking import LockLevel, LockOrder, run_in_transaction
from apps.core.models import DocumentSequence
from tests.integration.conftest import run_concurrently

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]


def test_both_sides_of_a_deadlock_complete() -> None:
    rows = [DocumentSequence.objects.create(scope=f"DL:{name}") for name in ("first", "second")]
    both_hold_one_lock = threading.Barrier(2)
    attempts = [0, 0]

    def work(index: int) -> Callable[[], int]:
        # Thread 0 locks first → second, thread 1 second → first: a lock cycle.
        own, other = (rows[0], rows[1]) if index == 0 else (rows[1], rows[0])

        def attempt() -> int:
            attempts[index] += 1
            LockOrder().lock(LockLevel.L9, DocumentSequence, [own.pk])
            if attempts[index] == 1:
                both_hold_one_lock.wait(timeout=10)
            LockOrder().lock(LockLevel.L9, DocumentSequence, [other.pk])
            DocumentSequence.objects.filter(pk=own.pk).update(next_value=own.next_value + 1)
            return attempts[index]

        return attempt

    results = run_concurrently(2, lambda index: run_in_transaction(work(index)))
    assert all(isinstance(result, int) for result in results), results
    # PostgreSQL aborted exactly one side with 40P01; the wrapper ran it again.
    assert sorted(attempts) == [1, 2]
    assert [row.next_value for row in DocumentSequence.objects.order_by("scope")] == [2, 2]
