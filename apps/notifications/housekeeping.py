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

A task runs in one transaction with one ``housekeeping.<name>`` authorization. A *batched* task
(:func:`register_batched_task`) is handed a ``run_step`` instead and calls it once per batch: the
worker runs each step in a transaction of its own, authorized again, so a long purge commits as
it goes and never holds its locks for the whole run.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.core.cache import caches
from django.db.models import Count, F, OuterRef, Q, QuerySet, Subquery, Window
from django.db.models.functions import Now, RowNumber

from apps.accounts.models import User
from apps.core.db import db_now
from apps.core.idempotency import purge_expired_records
from apps.notifications.models import EmailOutbox, InAppNotification, Status

logger = logging.getLogger(__name__)

IN_APP_TASK = "in_app_notifications"
PURGE_BATCH = 1000  # rows per DELETE: each statement holds its row locks only briefly
PURGE_BUDGET = 5.0  # seconds of one run; what is left waits for the next run

type Task = Callable[[], int]
type Step = Callable[[], int]  # one batch of a batched task: its own transaction
type RunStep = Callable[[Step], int]
type BatchedTask = Callable[[RunStep], int]

_TASKS: dict[str, Task] = {}
_BATCHED: dict[str, BatchedTask] = {}


def _check_name(name: str, task: object, registry: dict[str, Any], other: dict[str, Any]) -> None:
    if not name or "." in name:
        raise ValueError(f"Invalid housekeeping task name {name!r}")
    if registry.get(name, task) is not task or name in other:
        raise ValueError(f"Housekeeping task {name!r} is already registered")


def register_task(name: str, task: Task) -> None:
    """Add a task; ``name`` becomes the capability ``housekeeping.<name>``."""
    _check_name(name, task, _TASKS, _BATCHED)
    _TASKS[name] = task


def register_batched_task(name: str, task: BatchedTask) -> None:
    """Add a task that commits batch by batch: it receives ``run_step`` and calls it with each
    batch, which the worker runs in its own authorized transaction."""
    _check_name(name, task, _BATCHED, _TASKS)
    _BATCHED[name] = task


def tasks() -> dict[str, Task]:
    return dict(_TASKS)


def batched_tasks() -> dict[str, BatchedTask]:
    return dict(_BATCHED)


def run_inline(step: Step) -> int:
    """A ``run_step`` that runs the batch in the caller's transaction (tests, a direct call)."""
    return step()


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
    """The rows of each person beyond their ``IN_APP_MAX_PER_PERSON`` newest, read or not: how the
    candidates of a batch are found (a window over the crowded people only)."""
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


def _beyond_cap() -> Q:
    """The same rows as a condition on the row itself: older, by ``(notified_at, id)``, than the
    person's ``IN_APP_MAX_PER_PERSON``-th newest. The DELETE checks it on each row it removes."""
    cap = int(settings.IN_APP_MAX_PER_PERSON)
    last_kept = InAppNotification.objects.filter(user=OuterRef("user")).order_by(
        "-notified_at", "-pk"
    )[cap - 1 : cap]
    at = Subquery(last_kept.values("notified_at"))
    return Q(notified_at__lt=at) | Q(notified_at=at, pk__lt=Subquery(last_kept.values("pk")))


@dataclass(frozen=True)
class _Rule:
    """What one rule deletes. ``due`` is a condition on the row itself, which the DELETE states
    again next to the batch's ids: when the DELETE waits on a row another transaction changes
    (``in_app.notify`` refreshing a grouped notification), PostgreSQL checks the conditions of
    the DELETE again on the new version, never the subquery that picked the ids, so a row that
    no longer qualifies stays. ``candidates`` finds the batch's rows (by default, ``due``)."""

    due: Q
    candidates: QuerySet[InAppNotification] | None = None

    def batch(self, size: int) -> QuerySet[InAppNotification]:
        rows = InAppNotification.objects
        found = self.candidates if self.candidates is not None else rows.filter(self.due)
        return rows.filter(self.due, pk__in=found.values("pk")[:size])


def _purge_rules(now: datetime) -> dict[str, _Rule]:
    """What goes, by rule, in the order the rules run."""
    read_before = now - timedelta(days=int(settings.IN_APP_READ_RETENTION))
    unread_before = now - timedelta(days=int(settings.IN_APP_UNREAD_RETENTION))
    offboarded = User.objects.filter(offboarded_at__lt=read_before).values("pk")
    return {
        "offboarded": _Rule(Q(user__in=offboarded)),
        "read": _Rule(Q(read_at__lt=read_before)),
        "unread": _Rule(Q(read_at__isnull=True, notified_at__lt=unread_before)),
        "over_cap": _Rule(_beyond_cap(), _over_cap()),
    }


def _delete_batch(rule: _Rule) -> int:
    deleted, _details = rule.batch(PURGE_BATCH).delete()
    return deleted


def _delete_in_batches(rule: _Rule, budget: _Budget, run_step: RunStep) -> int:
    """``DELETE … WHERE due AND id IN (SELECT … LIMIT n)``, one step (one transaction under the
    worker) per batch, until none is left or the budget is spent."""
    deleted = 0
    while not budget.spent():
        batch = run_step(lambda: _delete_batch(rule))
        deleted += batch
        if batch < PURGE_BATCH:
            break
    return deleted


def purge_in_app_notifications(
    run_step: RunStep = run_inline, clock: Callable[[], float] = time.monotonic
) -> int:
    """Delete the in-app notifications Q98 says go: read ``IN_APP_READ_RETENTION`` days ago,
    unread for ``IN_APP_UNREAD_RETENTION`` days, beyond a person's ``IN_APP_MAX_PER_PERSON``
    newest, and every one of a person offboarded ``IN_APP_READ_RETENTION`` days ago.

    In batches of ``PURGE_BATCH`` within ``PURGE_BUDGET`` seconds, each run by ``run_step`` (the
    worker: its own transaction), so no long lock is held; what is left goes at the next run.
    Logs the count per rule, never who.
    """
    budget = _Budget(clock, clock() + PURGE_BUDGET)
    counts = {
        rule: _delete_in_batches(found, budget, run_step)
        for rule, found in _purge_rules(db_now()).items()
    }
    logger.info(
        "In-app notifications purged: %s", ", ".join(f"{rule}={n}" for rule, n in counts.items())
    )
    return sum(counts.values())


register_task("idempotency", purge_expired_records)
register_task("security_cache", purge_security_cache)
register_task("sensitive_bodies", expire_sensitive_bodies)
