"""The security cache: Django's database cache made safe for rate-limit counters (plan §5).

allauth keeps its rate-limit history and its used-TOTP markers in the default cache, and
serializes each counter with a lock taken through ``cache.add``. Django's ``DatabaseCache``
breaks that across processes: two writers can both "add" over an expired row, culling
deletes *live* rows once ``MAX_ENTRIES`` is exceeded (so key churn resets an attacker's own
counters), and a read deletes expired rows that a concurrent writer may just have renewed.

This backend keeps the ``django_cache`` table layout and replaces every data operation:

- ``add``, ``set``, ``incr`` and ``touch`` run under the row lock in one transaction, so
  ``add`` succeeds for exactly one writer and ``incr`` never loses an update;
- live entries are never evicted; only expired rows are deleted, and only while expired;
- a new key is admitted only while fewer than ``MAX_ENTRIES`` entries are live. At
  capacity the write raises ``CacheFull``, which the middleware turns into a 429. The lock
  companion of a live key (``<key>:lock``, allauth's convention) is always admitted, so
  existing counters keep working until they expire;
- values are JSON, never pickle, so a write to the table cannot become code execution;
- every entry expires: a value without a timeout would hold a slot forever;
- lock waits are bounded (``LOCK_TIMEOUT_MS``); a timeout fails closed as a 429.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from django.core.cache.backends.base import DEFAULT_TIMEOUT
from django.core.cache.backends.db import DatabaseCache
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError, connections, router, transaction
from django.utils import timezone

from apps.core.db import DEFAULT_LOCK_TIMEOUT_MS, LockTimeout, atomic_with_lock_timeout

if TYPE_CHECKING:
    from django.db.models import QuerySet

    from apps.core.models import CacheEntry

DEFAULT_CAPACITY = 50_000
LOCK_SUFFIX = ":lock"
# Serializes the admission of new keys so the capacity check cannot be raced.
ADMISSION_LOCK = "gmat:security-cache-admission"


class CacheUnavailable(Exception):
    """The security cache refused a write; authentication endpoints answer 429."""


class CacheFull(CacheUnavailable):
    """Capacity reached: no new key is admitted until live entries expire."""


class SecurityDatabaseCache(DatabaseCache):
    def __init__(self, table: str, params: dict[str, Any]) -> None:
        super().__init__(table, params)
        self._location = table
        options = params.get("OPTIONS", {})
        self._max_entries = int(options.get("MAX_ENTRIES", DEFAULT_CAPACITY))
        self._lock_timeout_ms = int(options.get("LOCK_TIMEOUT_MS", DEFAULT_LOCK_TIMEOUT_MS))
        if self._max_entries < 1 or self._lock_timeout_ms < 1:
            raise ImproperlyConfigured("SecurityDatabaseCache needs positive limits")

    # -- plumbing -----------------------------------------------------------------------

    def _rows(self) -> QuerySet[CacheEntry]:
        from apps.core.models import CacheEntry

        if CacheEntry._meta.db_table != self._location:
            raise ImproperlyConfigured(
                f"SecurityDatabaseCache LOCATION must be {CacheEntry._meta.db_table!r}"
            )
        return CacheEntry.objects.using(self._db())

    def _db(self) -> str:
        return router.db_for_write(self.cache_model_class)

    def _expires_at(self, timeout: float | None) -> datetime:
        """One clock for writing and for checking expiry: ``timezone.now``."""
        if timeout is DEFAULT_TIMEOUT:
            timeout = self.default_timeout
        if timeout is None:
            raise ValueError("The security cache does not store entries without an expiry")
        return timezone.now() + timedelta(seconds=timeout)

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """A transaction with a bounded lock wait; a timeout surfaces as ``CacheUnavailable``."""
        try:
            with atomic_with_lock_timeout(self._lock_timeout_ms, using=self._db()):
                yield
        except LockTimeout as exc:
            raise CacheUnavailable("security cache row is locked; try again") from exc

    def _admit(self, key: str, now: datetime) -> None:
        """Admit a new key or raise ``CacheFull``. Runs inside the write transaction."""
        rows = self._rows()
        with connections[self._db()].cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [ADMISSION_LOCK]
            )
        # Opportunistic housekeeping: expired rows never count and are safe to drop here.
        rows.filter(expires__lte=now).delete()
        if key.endswith(LOCK_SUFFIX):
            owner = key.removesuffix(LOCK_SUFFIX)
            if rows.filter(pk=owner, expires__gt=now).exists():
                return
        if rows.filter(expires__gt=now).count() >= self._max_entries:
            raise CacheFull(f"security cache at capacity ({self._max_entries} live entries)")

    def _write(self, key: str, value: Any, timeout: float | None, *, only_if_absent: bool) -> bool:
        expires = self._expires_at(timeout)
        if expires <= timezone.now():
            # A non-positive timeout stores nothing (Django semantics) and removes the key.
            if not only_if_absent:
                self._rows().filter(pk=key).delete()
            return False
        encoded = json.dumps(value, separators=(",", ":"))
        with self._transaction():
            rows = self._rows()
            row = rows.select_for_update().filter(pk=key).first()
            now = timezone.now()
            if row is not None and row.expires > now:
                if only_if_absent:
                    return False
                rows.filter(pk=key).update(value=encoded, expires=expires)
                return True
            if row is not None:
                # Expired: it no longer counts and is re-admitted like a new key.
                rows.filter(pk=key).delete()
            self._admit(key, now)
            try:
                with transaction.atomic(using=self._db()):
                    rows.create(cache_key=key, value=encoded, expires=expires)
            except IntegrityError:
                # A concurrent writer inserted the key after our read and has committed.
                if only_if_absent:
                    return False
                rows.filter(pk=key).update(value=encoded, expires=expires)
            return True

    # -- the cache API ------------------------------------------------------------------

    def get(self, key: Any, default: Any | None = None, version: int | None = None) -> Any:
        return self.get_many([key], version).get(key, default)

    def get_many(self, keys: Iterable[Any], version: int | None = None) -> dict[Any, Any]:
        key_map = {self.make_and_validate_key(key, version=version): key for key in keys}
        if not key_map:
            return {}
        rows = self._rows()
        now = timezone.now()
        result: dict[Any, Any] = {}
        expired: list[str] = []
        for cache_key, value, expires in rows.filter(pk__in=list(key_map)).values_list(
            "cache_key", "value", "expires"
        ):
            if expires > now:
                result[key_map[cache_key]] = json.loads(value)
            else:
                expired.append(cache_key)
        if expired:
            # Conditional: a writer may have renewed the row since it was read.
            rows.filter(pk__in=expired, expires__lte=now).delete()
        return result

    def set(
        self,
        key: Any,
        value: Any,
        timeout: float | None = DEFAULT_TIMEOUT,
        version: int | None = None,
    ) -> None:
        key = self.make_and_validate_key(key, version=version)
        self._write(key, value, timeout, only_if_absent=False)

    def add(
        self,
        key: Any,
        value: Any,
        timeout: float | None = DEFAULT_TIMEOUT,
        version: int | None = None,
    ) -> bool:
        key = self.make_and_validate_key(key, version=version)
        return self._write(key, value, timeout, only_if_absent=True)

    def touch(
        self, key: Any, timeout: float | None = DEFAULT_TIMEOUT, version: int | None = None
    ) -> bool:
        key = self.make_and_validate_key(key, version=version)
        expires = self._expires_at(timeout)
        rows = self._rows()
        if expires <= timezone.now():
            return bool(rows.filter(pk=key).delete()[0])
        with self._transaction():
            return bool(rows.filter(pk=key, expires__gt=timezone.now()).update(expires=expires))

    def incr(self, key: Any, delta: int = 1, version: int | None = None) -> int:
        key = self.make_and_validate_key(key, version=version)
        with self._transaction():
            rows = self._rows()
            row = rows.select_for_update().filter(pk=key).first()
            if row is None or row.expires <= timezone.now():
                raise ValueError(f"Key '{key}' not found")
            current = json.loads(row.value)
            if not isinstance(current, int) or isinstance(current, bool):
                raise TypeError(f"Key '{key}' does not hold an integer")
            new_value = current + delta
            rows.filter(pk=key).update(value=json.dumps(new_value))
            return new_value

    def has_key(self, key: Any, version: int | None = None) -> bool:
        key = self.make_and_validate_key(key, version=version)
        return self._rows().filter(pk=key, expires__gt=timezone.now()).exists()

    def delete(self, key: Any, version: int | None = None) -> bool:
        key = self.make_and_validate_key(key, version=version)
        return bool(self._rows().filter(pk=key).delete()[0])

    def delete_many(self, keys: Iterable[Any], version: int | None = None) -> None:
        cache_keys = [self.make_and_validate_key(key, version=version) for key in keys]
        if cache_keys:
            self._rows().filter(pk__in=cache_keys).delete()

    def clear(self) -> None:
        self._rows().delete()

    def purge_expired(self) -> int:
        """Delete expired rows; the worker's housekeeping calls this (M2.5)."""
        return self._rows().filter(expires__lte=timezone.now()).delete()[0]

    def _base_set(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("SecurityDatabaseCache writes through _write only")

    def _cull(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("SecurityDatabaseCache never culls live entries")
