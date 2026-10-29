"""SecurityDatabaseCache semantics (single connection; races are in tests/integration)."""

from collections.abc import Iterator
from datetime import timedelta
from unittest import mock

import pytest
from django.core.cache import caches
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings
from django.utils import timezone

from apps.core.cache import CacheFull, SecurityDatabaseCache
from apps.core.models import CacheEntry

pytestmark = pytest.mark.django_db

CAPACITY = 5


def small_cache(capacity: int = CAPACITY) -> dict[str, dict[str, object]]:
    return {
        "default": {
            "BACKEND": "apps.core.cache.SecurityDatabaseCache",
            "LOCATION": "django_cache",
            "OPTIONS": {"MAX_ENTRIES": capacity},
        }
    }


@pytest.fixture
def cache() -> Iterator[SecurityDatabaseCache]:
    with override_settings(CACHES=small_cache()):
        backend = caches["default"]
        assert isinstance(backend, SecurityDatabaseCache)
        yield backend


def later(seconds: float) -> mock._patch[mock.MagicMock]:
    """Move the cache's clock forward (expiry checks use timezone.now)."""
    moment = timezone.now() + timedelta(seconds=seconds)
    return mock.patch("apps.core.cache.timezone.now", return_value=moment)


class TestBasics:
    def test_set_get_and_json_values(self, cache: SecurityDatabaseCache) -> None:
        cache.set("history", [1.5, 2.5], 60)
        assert cache.get("history") == [1.5, 2.5]
        assert cache.get("missing", "fallback") == "fallback"
        assert cache.get_many(["history", "missing"]) == {"history": [1.5, 2.5]}
        assert cache.get_many([]) == {}
        raw = CacheEntry.objects.get().value
        assert raw == "[1.5,2.5]"

    def test_the_default_timeout_applies(self, cache: SecurityDatabaseCache) -> None:
        cache.set("key", 1)
        entry = CacheEntry.objects.get()
        assert str(entry) == ":1:key"
        remaining = (entry.expires - timezone.now()).total_seconds()
        assert 290 < remaining <= 300

    def test_values_that_are_not_json_are_refused(self, cache: SecurityDatabaseCache) -> None:
        with pytest.raises(TypeError):
            cache.set("object", object(), 60)

    def test_entries_must_expire(self, cache: SecurityDatabaseCache) -> None:
        with pytest.raises(ValueError, match="expiry"):
            cache.set("forever", 1, None)

    def test_a_non_positive_timeout_stores_nothing(self, cache: SecurityDatabaseCache) -> None:
        cache.set("key", 1, 60)
        cache.set("key", 2, 0)
        assert cache.get("key") is None
        assert cache.add("other", 1, -1) is False
        assert not CacheEntry.objects.exists()

    def test_add_only_when_absent(self, cache: SecurityDatabaseCache) -> None:
        assert cache.add("lock", True, 60) is True
        assert cache.add("lock", True, 60) is False
        with later(120):
            assert cache.add("lock", "again", 60) is True
        assert cache.get("lock") == "again"

    def test_set_overwrites_a_live_entry(self, cache: SecurityDatabaseCache) -> None:
        cache.set("key", 1, 60)
        cache.set("key", 2, 60)
        assert cache.get("key") == 2

    def test_incr_and_decr(self, cache: SecurityDatabaseCache) -> None:
        cache.set("count", 1, 60)
        assert cache.incr("count") == 2
        assert cache.incr("count", 5) == 7
        assert cache.decr("count", 3) == 4
        with pytest.raises(ValueError, match="not found"):
            cache.incr("missing")
        cache.set("text", "x", 60)
        with pytest.raises(TypeError):
            cache.incr("text")
        cache.set("flag", True, 60)
        with pytest.raises(TypeError):
            cache.incr("flag")

    def test_incr_of_an_expired_entry_fails(self, cache: SecurityDatabaseCache) -> None:
        cache.set("count", 1, 60)
        with later(120), pytest.raises(ValueError):
            cache.incr("count")

    def test_touch_has_key_delete(self, cache: SecurityDatabaseCache) -> None:
        cache.set("key", 1, 60)
        assert cache.has_key("key")
        assert cache.touch("key", 600) is True
        with later(300):
            assert cache.has_key("key")
        assert cache.touch("missing", 60) is False
        assert cache.touch("key", 0) is True
        assert not cache.has_key("key")
        cache.set("a", 1, 60)
        cache.set("b", 1, 60)
        assert cache.delete("a") is True
        assert cache.delete("a") is False
        cache.delete_many(["b"])
        cache.delete_many([])
        assert not CacheEntry.objects.exists()

    def test_clear_and_purge(self, cache: SecurityDatabaseCache) -> None:
        cache.set("short", 1, 1)
        cache.set("long", 1, 600)
        with later(10):
            assert cache.purge_expired() == 1
        assert list(CacheEntry.objects.values_list("pk", flat=True)) == [":1:long"]
        cache.clear()
        assert not CacheEntry.objects.exists()

    def test_a_read_removes_only_expired_rows(self, cache: SecurityDatabaseCache) -> None:
        cache.set("short", 1, 1)
        with later(10):
            assert cache.get("short") is None
        assert not CacheEntry.objects.exists()

    def test_inherited_pickle_paths_are_disabled(self, cache: SecurityDatabaseCache) -> None:
        with pytest.raises(NotImplementedError):
            cache._base_set("set", "k", 1)
        with pytest.raises(NotImplementedError):
            cache._cull(None, None, None, 0)


class TestCapacity:
    def fill(self, cache: SecurityDatabaseCache, count: int = CAPACITY) -> None:
        for number in range(count):
            cache.set(f"counter-{number}", [number], 300)

    def test_a_new_key_is_refused_at_capacity(self, cache: SecurityDatabaseCache) -> None:
        self.fill(cache)
        with pytest.raises(CacheFull):
            cache.set("newcomer", 1, 60)
        with pytest.raises(CacheFull):
            cache.add("newcomer", 1, 60)
        assert CacheEntry.objects.count() == CAPACITY

    def test_existing_counters_survive_and_keep_working(self, cache: SecurityDatabaseCache) -> None:
        self.fill(cache)
        cache.set("counter-0", [0, 1], 300)
        assert cache.get("counter-0") == [0, 1]
        # allauth serializes each counter with "<key>:lock": admitted for a live key.
        assert cache.add("counter-0:lock", True, 10) is True
        assert cache.delete("counter-0:lock") is True
        with pytest.raises(CacheFull):
            cache.add("unknown:lock", True, 10)
        assert {f"counter-{n}" for n in range(CAPACITY)} == {
            key.removeprefix(":1:") for key in CacheEntry.objects.values_list("pk", flat=True)
        }

    def test_rows_free_up_after_expiry(self, cache: SecurityDatabaseCache) -> None:
        self.fill(cache)
        with later(600):
            cache.set("newcomer", 1, 60)
            assert cache.get("newcomer") == 1
        assert CacheEntry.objects.count() == 1

    def test_an_expired_key_is_re_admitted_like_a_new_one(
        self, cache: SecurityDatabaseCache
    ) -> None:
        cache.set("old", 1, 1)
        with later(10):
            self.fill(cache)
            with pytest.raises(CacheFull):
                cache.set("old", 2, 60)

    def test_counters_survive_key_churn(self, cache: SecurityDatabaseCache) -> None:
        """Rotating emails or IPs must not evict an attacker's own live counter."""
        cache.set("victim-counter", [1.0, 2.0, 3.0], 300)
        refused = 0
        for number in range(50):
            try:
                cache.set(f"churn-{number}", [number], 300)
            except CacheFull:
                refused += 1
        assert refused == 50 - (CAPACITY - 1)
        assert cache.get("victim-counter") == [1.0, 2.0, 3.0]

    def test_counters_survive_churn_of_short_lived_keys(self, cache: SecurityDatabaseCache) -> None:
        cache.set("victim-counter", [1.0], 3600)
        for number in range(50):
            with later(number * 10):
                cache.set(f"churn-{number}", [number], 5)
        assert cache.get("victim-counter") == [1.0]


class TestConfiguration:
    def test_location_must_be_the_migrated_table(self) -> None:
        backend = SecurityDatabaseCache("other_table", {})
        with pytest.raises(ImproperlyConfigured, match="django_cache"):
            backend.get("key")

    @pytest.mark.parametrize("options", [{"MAX_ENTRIES": 0}, {"LOCK_TIMEOUT_MS": 0}])
    def test_limits_must_be_positive(self, options: dict[str, int]) -> None:
        with pytest.raises(ImproperlyConfigured):
            SecurityDatabaseCache("django_cache", {"OPTIONS": options})

    def test_default_capacity(self) -> None:
        assert SecurityDatabaseCache("django_cache", {})._max_entries == 50_000
