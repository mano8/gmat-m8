"""A reviewer's edit refreshing the requester's unread notification while the requester marks
everything read, or while the worker purges old notifications (M17.3, Q98).

``in_app.notify`` locks the unread row it refreshes, so a *mark all as read* that arrives
meanwhile waits and then marks the refreshed row read; a *mark all as read* that commits first
leaves nothing unread, and the edit then adds a new row. Either way the person ends with the edit
told or read, never lost, and never two unread rows about one request.

The purge's DELETE that waits on a row the edit refreshes checks its rule again on the refreshed
row and leaves it; and under the worker each batch of the purge is a transaction of its own.
"""

import time
from datetime import timedelta
from typing import Any

import pytest
from django.db import transaction
from django.utils import timezone

from apps.access.principals import SystemPrincipal
from apps.material_requests.models import MaterialRequest
from apps.notifications import housekeeping, in_app
from apps.notifications.kinds import SUBJECT, Notice
from apps.notifications.models import InAppNotification
from apps.notifications.services import mark_all_read, run_housekeeping
from tests.integration.conftest import run_concurrently
from tests.org import Org, People
from tests.support import principal_for

pytestmark = [pytest.mark.integration, pytest.mark.serial, pytest.mark.django_db(transaction=True)]

HOLD = 0.4  # seconds the edit keeps its locks
HEAD_START = 0.15  # seconds before the second operation starts


@pytest.fixture
def request_(org: Org, people: People) -> MaterialRequest:
    return MaterialRequest.objects.create(
        project=org.hospital,
        procurement_branch_id=org.hospital.procurement_branch_id,
        requested_by=people.tech_mad,
        status=MaterialRequest.Status.DRAFT,  # the requester sees it; enough for the race
    )


def _edit(people: People, request: MaterialRequest, hold: float) -> int:
    with transaction.atomic():
        told = in_app.notify(Notice.REQUEST_EDITED, [people.tech_mad], request)
        time.sleep(hold)
    return told


def _run(first: Any, second: Any) -> list[Any]:
    def run(index: int) -> Any:
        if index:
            time.sleep(HEAD_START)
            return second()
        return first()

    return run_concurrently(2, run)


def test_mark_all_read_waits_for_the_refresh(people: People, request_: MaterialRequest) -> None:
    InAppNotification.objects.create(
        user=people.tech_mad,
        notice=Notice.REQUEST_EDITED,
        object_type=SUBJECT,
        object_id=request_.pk,
    )
    results = _run(
        lambda: _edit(people, request_, HOLD),
        lambda: mark_all_read(principal_for(people.tech_mad)),
    )
    assert results == [1, 1], results
    (row,) = InAppNotification.objects.all()
    assert row.read_at is not None  # the refreshed row, read after the edit committed


def test_an_edit_after_everything_was_read_adds_a_row(
    people: People, request_: MaterialRequest
) -> None:
    InAppNotification.objects.create(
        user=people.tech_mad,
        notice=Notice.REQUEST_EDITED,
        object_type=SUBJECT,
        object_id=request_.pk,
    )

    def read_slowly() -> int:
        with transaction.atomic():
            marked = mark_all_read(principal_for(people.tech_mad))
            time.sleep(HOLD)
        return marked

    results = _run(read_slowly, lambda: _edit(people, request_, 0))
    assert results == [1, 1], results
    rows = list(InAppNotification.objects.order_by("notified_at", "pk"))
    assert len(rows) == 2
    assert [row.read_at is None for row in rows].count(True) == 1


def _old(people: People, request: MaterialRequest, count: int = 1) -> list[InAppNotification]:
    """Unread *changed by a reviewer* rows of the requester, due for the purge (100 days old)."""
    long_ago = timezone.now() - timedelta(days=100)
    return InAppNotification.objects.bulk_create(
        InAppNotification(
            user=people.tech_mad,
            notice=Notice.REQUEST_EDITED if index == 0 else Notice.REQUEST_HANDED_OVER,
            object_type=SUBJECT,
            object_id=request.pk,
            notified_at=long_ago - timedelta(seconds=index),
        )
        for index in range(count)
    )


def test_the_purge_leaves_a_row_an_edit_refreshed(
    people: People, request_: MaterialRequest
) -> None:
    (row,) = _old(people, request_)
    results = _run(
        lambda: _edit(people, request_, HOLD),
        housekeeping.purge_in_app_notifications,
    )
    assert results == [1, 0], results  # the DELETE waited, then saw the row was new again
    (kept,) = InAppNotification.objects.all()
    assert kept.pk == row.pk and kept.notified_at > timezone.now() - timedelta(minutes=1)
    assert kept.updated_at > row.updated_at


def test_under_the_worker_each_batch_commits(
    people: People, request_: MaterialRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure in the third batch keeps what the first two deleted."""
    monkeypatch.setattr(housekeeping, "PURGE_BATCH", 2)
    monkeypatch.setattr(housekeeping, "_TASKS", {})
    _old(people, request_, 5)
    real = housekeeping._delete_batch
    calls: list[int] = []

    def failing(rule: Any) -> int:
        calls.append(1)
        if len(calls) == 5:  # offboarded, read, then the unread rule's 2 + 2, then this
            raise RuntimeError("the database went away")
        return real(rule)

    monkeypatch.setattr(housekeeping, "_delete_batch", failing)
    with pytest.raises(RuntimeError):
        run_housekeeping(SystemPrincipal())
    assert InAppNotification.objects.count() == 1
