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
and the media purge, which runs only while it holds the ``gmat:media-backup`` lock).
"""

from collections.abc import Callable

from django.core.cache import caches
from django.db.models.functions import Now

from apps.core.idempotency import purge_expired_records
from apps.notifications.models import EmailOutbox, Status

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


register_task("idempotency", purge_expired_records)
register_task("security_cache", purge_security_cache)
register_task("sensitive_bodies", expire_sensitive_bodies)
