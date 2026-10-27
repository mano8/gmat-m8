"""Helpers for the concurrency tests: real threads, one database connection each."""

import threading
from collections.abc import Callable
from typing import Any

from django.db import connections


def run_concurrently(count: int, work: Callable[[int], Any], timeout: float = 60) -> list[Any]:
    """Start ``count`` threads at the same instant; return each result or raised exception.

    Each thread has its own database connection and its own cache client (Django keeps
    both per thread), like separate Gunicorn workers.
    """
    barrier = threading.Barrier(count)
    results: list[Any] = [None] * count

    def run(index: int) -> None:
        try:
            barrier.wait(timeout)
            results[index] = work(index)
        except Exception as exc:  # reported to the test, which asserts on it
            results[index] = exc
        finally:
            connections.close_all()

    threads = [threading.Thread(target=run, args=(index,)) for index in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout)
        assert not thread.is_alive(), "a worker thread hung"
    return results
