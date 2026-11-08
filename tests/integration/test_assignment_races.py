"""Reviewer assignment racing a change to the assignee's authority (M7b.2, S8, plan §4.7).

The assignment locks the assignee's ``User`` row ``FOR SHARE`` at L0 with the actor's;
a membership change locks it ``FOR NO KEY UPDATE`` first. So an assignment that starts
while the assignee's membership is being revoked waits for the revocation and is refused,
and a revocation that starts while an assignment holds its locks waits for it: the request
is then assigned to someone who reads as unassigned, with nothing rewritten.

The first operation is slowed inside its transaction (a subscriber of its own event sleeps
while its locks are held); the second starts while it waits.
"""

import time
from collections.abc import Callable, Iterator
from decimal import Decimal
from typing import Any

import pytest
from django.core.exceptions import ValidationError

from apps.catalog.models import Material, UnitOfMeasure
from apps.core import events
from apps.material_requests import reviewers, services
from apps.material_requests.models import MaterialRequest, MaterialRequestLine
from apps.organizations import services as org_services
from apps.organizations.models import Membership
from tests.integration.conftest import run_concurrently
from tests.org import Org, People
from tests.support import principal_for

pytestmark = [pytest.mark.integration, pytest.mark.serial, pytest.mark.django_db(transaction=True)]

HOLD = 0.4  # seconds the first operation keeps its locks
HEAD_START = 0.15  # seconds before the second operation starts


def second_starts_later(*operations: Callable[[], Any]) -> list[Any]:
    def run(index: int) -> Any:
        if index:
            time.sleep(HEAD_START)
        return operations[index]()

    return run_concurrently(len(operations), run)


@pytest.fixture
def slowly() -> Iterator[Callable[[str], None]]:
    """``slowly(event)``: the publisher of ``event`` holds its locks for ``HOLD``."""
    registered: list[Callable[[], None]] = []

    def hold(event: events.Event) -> None:
        time.sleep(HOLD)

    yield lambda name: registered.append(events.subscribe(name, hold))
    for unsubscribe in registered:
        unsubscribe()


def submitted(org: Org, people: People) -> MaterialRequest:
    """A submitted hospital request by A-MAD's technician (A-MAD buys for it)."""
    unit, _ = UnitOfMeasure.objects.get_or_create(code="M", defaults={"name": "m", "decimals": 2})
    cable = Material.objects.create(reference="CAB-25", name="Cable 2,5", unit=unit)
    request = MaterialRequest.objects.create(
        project=org.hospital,
        procurement_branch=org.a_mad,
        requested_by=people.tech_mad,
        status=MaterialRequest.Status.SUBMITTED,
        number="SOL-T-00001",
        submitted_at=org.hospital.created_at,
    )
    MaterialRequestLine.objects.create(
        request=request,
        project=org.hospital,
        line_no=1,
        material=cable,
        unit=unit,
        requested_qty=Decimal("10"),
    )
    return request


def revoke(people: People) -> Callable[[], Any]:
    membership = Membership.objects.get(user=people.engineer)
    return lambda: org_services.revoke_membership(principal_for(people.group_admin), membership.pk)


def assign(people: People, request: MaterialRequest) -> Callable[[], Any]:
    return lambda: services.assign_reviewer(
        principal_for(people.mm_mad), request.pk, reviewer_id=people.engineer.pk, note="Yours"
    )


def test_an_assignment_waits_for_the_revocation_and_is_refused(
    org: Org, people: People, slowly: Callable[[str], None]
) -> None:
    request = submitted(org, people)
    slowly("membership.revoked")
    revoked, assigned = second_starts_later(revoke(people), assign(people, request))
    assert isinstance(revoked, Membership)
    assert isinstance(assigned, ValidationError)
    assert MaterialRequest.objects.get(pk=request.pk).assigned_reviewer_id is None


def test_a_revocation_after_the_assignment_leaves_the_request_unassigned(
    org: Org, people: People, slowly: Callable[[str], None]
) -> None:
    request = submitted(org, people)
    slowly("request.reviewer_assigned")
    assigned, revoked = second_starts_later(assign(people, request), revoke(people))
    assert isinstance(assigned, MaterialRequest)
    assert isinstance(revoked, Membership)
    stored = MaterialRequest.objects.get(pk=request.pk)
    assert stored.assigned_reviewer_id == people.engineer.pk  # nothing rewritten
    assert reviewers.assignee(stored) is None
