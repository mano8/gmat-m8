"""Hourly housekeeping tasks of the worker (plan §5).

Each task is a function that writes and returns how many rows it touched. The worker runs
them through ``apps.notifications.services.run_housekeeping``, one transaction per task,
each authorized as ``housekeeping.<name>`` for the worker's ``SystemPrincipal``.

Built in:

- ``idempotency``: idempotency records older than 7 days (``IDEMPOTENCY_RECORD_RETENTION``);
- ``security_cache``: expired rows of the security cache;
- ``sensitive_bodies``: sensitive bodies past ``expires_at`` are wiped; a message still
  ``QUEUED`` then becomes ``EXPIRED`` (the user asks again).

Later steps register theirs from ``AppConfig.ready`` (M6.4: expired attachment reservations,
and the media purge, which runs only while it holds the ``gmat:media-backup`` lock; M17.3: the
purge of in-app notifications, :func:`purge_in_app_notifications`, registered by this app's
``ready``).
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.conf import settings
from django.core.cache import caches
from django.db.models import Count, F, QuerySet, Window
from django.db.models.functions import Now, RowNumber

from apps.core.db import db_now
from apps.core.idempotency import purge_expired_records
from apps.notifications.models import EmailOutbox, InAppNotification, Status

logger = logging.getLogger(__name__)

IN_APP_TASK = "in_app_notifications"
PURGE_BATCH = 1000  # rows per DELETE: each statement holds its row locks only briefly
PURGE_BUDGET = 5.0  # seconds of one run; what is left waits for the next run

type Task = Callable[[], int]

_TASKS: dict[str, Task] = {}


def register_task(name: str, task: Task) -> None:
    """Add a task; ``name`` becomes the capability ``housekeeping.<name>``."""
    if not name or "." in name:
        raise ValueError(f"Invalid housekeeping task name {name!r}")
    if _TASKS.get(name, task) is not task:
        raise ValueError(f"Housekeeping task {name!r} is already registered")
    _TASKS[name] = task


def tasks() -> dict[str, Task]:
    return dict(_TASKS)


def purge_security_cache() -> int:
    cache = caches["default"]
    purge = getattr(cache, "purge_expired", None)
    return int(purge()) if purge is not None else 0


def expire_sensitive_bodies() -> int:
    """Wipe every sensitive body past its expiry; unsent queued messages become ``EXPIRED``.

    A message being sent right now (``SENDING``) is left to its outcome. Plain ``UPDATE``s:
    a claim that locked a row first makes PostgreSQL re-check the row's new status.
    """
    expired = EmailOutbox.objects.filter(
        sensitive=True, wiped_at__isnull=True, expires_at__lte=Now()
    ).exclude(status=Status.SENDING)
    wiped = {"body": "", "html_body": "", "wiped_at": Now()}
    count = expired.filter(status=Status.QUEUED).update(status=Status.EXPIRED, **wiped)
    return count + expired.update(**wiped)


# --- in-app notifications (M17.3, Q98) -----------------------------------------------------


@dataclass(frozen=True)
class _Budget:
    clock: Callable[[], float]
    deadline: float

    def spent(self) -> bool:
        return self.clock() >= self.deadline


def _over_cap() -> QuerySet[InAppNotification]:
    """The rows of each person beyond their ``IN_APP_MAX_PER_PERSON`` newest, read or not."""
    cap = int(settings.IN_APP_MAX_PER_PERSON)
    crowded = (
        InAppNotification.objects.values("user")
        .annotate(rows=Count("pk"))
        .filter(rows__gt=cap)
        .values("user")
    )
    newest_first = (F("notified_at").desc(), F("pk").desc())
    return (
        InAppNotification.objects.filter(user__in=crowded)
        .annotate(rank=Window(RowNumber(), partition_by=F("user"), order_by=newest_first))
        .filter(rank__gt=cap)
    )


def _purge_rules(now: datetime) -> dict[str, QuerySet[InAppNotification]]:
    """What goes, by rule, in the order the rules run."""
    read_before = now - timedelta(days=int(settings.IN_APP_READ_RETENTION))
    unread_before = now - timedelta(days=int(settings.IN_APP_UNREAD_RETENTION))
    rows = InAppNotification.objects
    return {
        "offboarded": rows.filter(user__offboarded_at__lt=read_before),
        "read": rows.filter(read_at__lt=read_before),
        "unread": rows.filter(read_at__isnull=True, notified_at__lt=unread_before),
        "over_cap": _over_cap(),
    }


def _delete_in_batches(rows: QuerySet[InAppNotification], budget: _Budget) -> int:
    """``DELETE … WHERE id IN (SELECT … LIMIT n)`` until none is left or the budget is spent."""
    deleted = 0
    while not budget.spent():
        batch, _details = InAppNotification.objects.filter(
            pk__in=rows.values("pk")[:PURGE_BATCH]
        ).delete()
        deleted += batch
        if batch < PURGE_BATCH:
            break
    return deleted


def purge_in_app_notifications(clock: Callable[[], float] = time.monotonic) -> int:
    """Delete the in-app notifications Q98 says go: read ``IN_APP_READ_RETENTION`` days ago,
    unread for ``IN_APP_UNREAD_RETENTION`` days, beyond a person's ``IN_APP_MAX_PER_PERSON``
    newest, and every one of a person offboarded ``IN_APP_READ_RETENTION`` days ago.

    In batches of ``PURGE_BATCH`` within ``PURGE_BUDGET`` seconds, so no long lock is held; what
    is left goes at the next run. Logs the count per rule, never who.
    """
    budget = _Budget(clock, clock() + PURGE_BUDGET)
    counts = {
        rule: _delete_in_batches(rows, budget) for rule, rows in _purge_rules(db_now()).items()
    }
    logger.info(
        "In-app notifications purged: %s", ", ".join(f"{rule}={n}" for rule, n in counts.items())
    )
    return sum(counts.values())


register_task("idempotency", purge_expired_records)
register_task("security_cache", purge_security_cache)
register_task("sensitive_bodies", expire_sensitive_bodies)
