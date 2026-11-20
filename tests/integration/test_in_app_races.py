"""A reviewer's edit refreshing the requester's unread notification while the requester marks
everything read (M17.3, Q98).

``in_app.notify`` locks the unread row it refreshes, so a *mark all as read* that arrives
meanwhile waits and then marks the refreshed row read; a *mark all as read* that commits first
leaves nothing unread, and the edit then adds a new row. Either way the person ends with the edit
told or read, never lost, and never two unread rows about one request.
"""

import time
from typing import Any

import pytest
from django.db import transaction

from apps.material_requests.models import MaterialRequest
from apps.notifications import in_app
from apps.notifications.kinds import SUBJECT, Notice
from apps.notifications.models import InAppNotification
from apps.notifications.services import mark_all_read
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
