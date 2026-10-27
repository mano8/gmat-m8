"""SecurityDatabaseCache under concurrency: separate connections and cache clients."""

import threading
from collections.abc import Iterator
from datetime import timedelta

import pytest
from django.core.cache import caches
from django.db import transaction
from django.test import override_settings
from django.utils import timezone

from apps.core.cache import CacheFull, CacheUnavailable
from apps.core.models import CacheEntry
from tests.integration.conftest import run_concurrently

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]

WORKERS = 8


def cache_settings(capacity: int = 1000, lock_timeout_ms: int = 5000) -> dict[str, object]:
    return {
        "default": {
            "BACKEND": "apps.core.cache.SecurityDatabaseCache",
            "LOCATION": "django_cache",
            "OPTIONS": {"MAX_ENTRIES": capacity, "LOCK_TIMEOUT_MS": lock_timeout_ms},
        }
    }


@pytest.fixture
def shared_settings() -> Iterator[None]:
    with override_settings(CACHES=cache_settings()):
        yield


def test_add_succeeds_for_exactly_one_writer(shared_settings: None) -> None:
    results = run_concurrently(WORKERS, lambda index: caches["default"].add("lock", index, 30))
    assert results.count(True) == 1
    assert results.count(False) == WORKERS - 1


def test_add_over_an_expired_row_succeeds_for_exactly_one_writer(shared_settings: None) -> None:
    """Django's DatabaseCache lets every writer "add" over an expired row."""
    CacheEntry.objects.create(
        cache_key=":1:lock", value="true", expires=timezone.now() - timedelta(seconds=5)
    )
    results = run_concurrently(WORKERS, lambda index: caches["default"].add("lock", index, 30))
    assert results.count(True) == 1


def test_incr_never_loses_an_update(shared_settings: None) -> None:
    caches["default"].set("count", 0, 60)
    results = run_concurrently(WORKERS, lambda index: caches["default"].incr("count"))
    assert sorted(results) == list(range(1, WORKERS + 1))
    assert caches["default"].get("count") == WORKERS


def test_concurrent_sets_of_a_new_key_all_succeed(shared_settings: None) -> None:
    results = run_concurrently(WORKERS, lambda index: caches["default"].set("key", index, 60))
    assert results == [None] * WORKERS
    assert caches["default"].get("key") in range(WORKERS)
    assert CacheEntry.objects.count() == 1


def test_capacity_holds_under_concurrency() -> None:
    with override_settings(CACHES=cache_settings(capacity=3)):
        results = run_concurrently(
            WORKERS, lambda index: caches["default"].add(f"key-{index}", index, 60)
        )
        assert results.count(True) == 3
        assert sum(isinstance(result, CacheFull) for result in results) == WORKERS - 3
        assert CacheEntry.objects.count() == 3


def test_a_lock_wait_is_bounded_and_fails_closed() -> None:
    with override_settings(CACHES=cache_settings(lock_timeout_ms=200)):
        caches["default"].set("count", 0, 60)
        locked, release = threading.Event(), threading.Event()

        def hold_the_row(index: int) -> None:
            with transaction.atomic():
                CacheEntry.objects.select_for_update().get(cache_key=":1:count")
                locked.set()
                release.wait(10)

        holder = threading.Thread(target=run_concurrently, args=(1, hold_the_row))
        holder.start()
        try:
            assert locked.wait(10)
            with pytest.raises(CacheUnavailable):
                caches["default"].incr("count")
        finally:
            release.set()
            holder.join(10)
        assert caches["default"].incr("count") == 1
