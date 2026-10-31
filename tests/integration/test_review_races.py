"""Review and closing races with real connections (plan §4.7, §4.3, §4.9).

- Two reviewers deciding the last two lines at once: both decisions commit and the request
  is finalised exactly once, by whichever comes second (the header lock, L2, serializes).
- A withdrawal racing a decision: whichever commits first wins and the other is refused.
- A line closed short racing a manual close keeps its own provenance (``LINE``); the
  manual close closes only what is still live.

The first operation is slowed inside its transaction (a subscriber of its own event sleeps
while its locks are held); the second starts while it waits.
"""

import time
from collections.abc import Callable, Iterator
from decimal import Decimal
from typing import Any

import pytest

from apps.audit.models import AuditEvent
from apps.catalog.models import Material, UnitOfMeasure
from apps.core import events
from apps.material_requests import services
from apps.material_requests.models import MaterialRequest, MaterialRequestLine
from tests.integration.conftest import run_concurrently
from tests.org import Org, People
from tests.support import principal_for

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]

Status = MaterialRequest.Status
HOLD = 0.4  # seconds the first operation keeps its locks
HEAD_START = 0.15  # seconds before the second operation starts


def outcome(results: list[Any]) -> list[str]:
    return [type(result).__name__ for result in results]


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


def make(org: Org, people: People, status: str, *, decided: bool) -> MaterialRequest:
    """A hospital request by A-MAD's technician with two cable lines."""
    unit, _ = UnitOfMeasure.objects.get_or_create(code="M", defaults={"name": "m", "decimals": 2})
    cable = Material.objects.create(reference="CAB-25", name="Cable 2,5", unit=unit)
    request = MaterialRequest.objects.create(
        project=org.hospital,
        procurement_branch=org.a_mad,
        requested_by=people.tech_mad,
        status=status,
        number="SOL-T-00001",
        submitted_at=org.hospital.created_at,
    )
    for line_no in (1, 2):
        MaterialRequestLine.objects.create(
            request=request,
            project=org.hospital,
            line_no=line_no,
            material=cable,
            unit=unit,
            requested_qty=Decimal("10"),
            **(
                {
                    "decision": "APPROVED",
                    "approved_qty": Decimal("10"),
                    "decided_by": people.mm_mad,
                    "decided_at": request.created_at,
                }
                if decided
                else {}
            ),
        )
    return request


def line(request: MaterialRequest, line_no: int) -> Any:
    return MaterialRequestLine.objects.get(request=request, line_no=line_no)


def test_two_reviewers_finalise_once(
    org: Org, people: People, slowly: Callable[[str], None]
) -> None:
    request = make(org, people, Status.SUBMITTED, decided=False)
    slowly("request_line.decided")
    results = second_starts_later(
        lambda: services.decide_line(
            principal_for(people.mm_mad), line(request, 1).pk, decision="APPROVED"
        ),
        lambda: services.decide_line(
            principal_for(people.site_manager),
            line(request, 2).pk,
            decision="REJECTED",
            note="No",
        ),
    )
    assert outcome(results) == ["MaterialRequestLine", "MaterialRequestLine"]
    assert MaterialRequest.objects.get(pk=request.pk).status == Status.APPROVED
    assert AuditEvent.objects.filter(action="request.approved").count() == 1
    assert [line(request, n).decision for n in (1, 2)] == ["APPROVED", "REJECTED"]


def test_a_double_approve_all_finalises_once(org: Org, people: People) -> None:
    request = make(org, people, Status.SUBMITTED, decided=False)
    principal = principal_for(people.mm_mad)
    results = run_concurrently(2, lambda index: services.approve_all(principal, request.pk))
    assert sorted(outcome(results)) == ["MaterialRequest", "ValidationError"]
    assert AuditEvent.objects.filter(action="request.approved").count() == 1


def test_a_withdrawal_waits_for_a_decision_and_is_refused(
    org: Org, people: People, slowly: Callable[[str], None]
) -> None:
    request = make(org, people, Status.SUBMITTED, decided=False)
    slowly("request_line.decided")
    results = second_starts_later(
        lambda: services.decide_line(
            principal_for(people.mm_mad), line(request, 1).pk, decision="APPROVED"
        ),
        lambda: services.withdraw(principal_for(people.tech_mad), request.pk),
    )
    assert outcome(results) == ["MaterialRequestLine", "ValidationError"]
    assert results[1].code == "decided"
    assert MaterialRequest.objects.get(pk=request.pk).status == Status.SUBMITTED


def test_a_decision_waits_for_a_withdrawal_and_is_refused(
    org: Org, people: People, slowly: Callable[[str], None]
) -> None:
    request = make(org, people, Status.SUBMITTED, decided=False)
    slowly("request.withdrawn")
    results = second_starts_later(
        lambda: services.withdraw(principal_for(people.tech_mad), request.pk),
        lambda: services.decide_line(
            principal_for(people.mm_mad), line(request, 1).pk, decision="APPROVED"
        ),
    )
    assert outcome(results) == ["MaterialRequest", "ValidationError"]
    assert results[1].code == "transition"
    assert line(request, 1).decision == "PENDING"


def test_a_manual_close_waits_for_a_line_closure(
    org: Org, people: People, slowly: Callable[[str], None]
) -> None:
    request = make(org, people, Status.APPROVED, decided=True)
    principal = principal_for(people.mm_mad)
    slowly("request_line.closed_short")
    results = second_starts_later(
        lambda: services.close_line_short(principal, line(request, 1).pk, reason="Por línea"),
        lambda: services.manual_close(principal, request.pk, reason="Fin"),
    )
    assert outcome(results) == ["MaterialRequestLine", "MaterialRequest"]
    assert [line(request, n).closed_short_source for n in (1, 2)] == ["LINE", "REQUEST"]
    services.reopen(principal, request.pk, reason="x")
    assert [line(request, n).closed_short_source for n in (1, 2)] == ["LINE", ""]


def test_a_line_closure_waits_for_a_manual_close_and_is_refused(
    org: Org, people: People, slowly: Callable[[str], None]
) -> None:
    request = make(org, people, Status.APPROVED, decided=True)
    principal = principal_for(people.mm_mad)
    slowly("request.closed")
    results = second_starts_later(
        lambda: services.manual_close(principal, request.pk, reason="Fin"),
        lambda: services.close_line_short(principal, line(request, 1).pk, reason="x"),
    )
    assert outcome(results) == ["MaterialRequest", "ValidationError"]
    assert results[1].code == "transition"
    assert [line(request, n).closed_short_source for n in (1, 2)] == ["REQUEST", "REQUEST"]
