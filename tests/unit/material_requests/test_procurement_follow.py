"""Open requests follow their project's procurement branch, or the change is refused."""

from decimal import Decimal
from uuid import UUID

import pytest
from django.core.exceptions import ValidationError

from apps.audit.models import AuditEvent
from apps.material_requests import ledger
from apps.material_requests.models import MaterialRequest
from apps.projects import procurement, services
from apps.projects.models import Project
from tests.org import Org, People
from tests.support import principal_for
from tests.unit.material_requests.conftest import MakeRequest

pytestmark = pytest.mark.django_db

Status = MaterialRequest.Status


def _move(people: People, project: Project, branch_id: UUID) -> Project:
    return services.update_project(
        principal_for(people.group_admin), project.pk, procurement_branch_id=branch_id
    )


def _branch_of(request: MaterialRequest) -> UUID:
    return MaterialRequest.objects.values_list("procurement_branch_id", flat=True).get(
        pk=request.pk
    )


def test_open_requests_move_with_the_project(
    org: Org, people: People, make_request: MakeRequest
) -> None:
    moving = [
        make_request(Status.DRAFT),
        make_request(Status.SUBMITTED),
        make_request(Status.APPROVED, decided=True),
    ]
    staying = [make_request(status) for status in (Status.REJECTED, Status.CANCELLED)]
    staying.append(make_request(Status.CLOSED, decided=True))

    _move(people, org.hospital, org.b_mad.pk)

    assert {_branch_of(request) for request in moving} == {org.b_mad.pk}
    assert {_branch_of(request) for request in staying} == {org.a_mad.pk}
    events = AuditEvent.objects.filter(action="request.procurement_branch_changed")
    assert {event.object_id for event in events} == {request.pk for request in moving}
    event = events.first()
    assert event is not None
    assert event.branch_id == org.b_mad.pk
    assert event.data["from"] == str(org.a_mad.pk)
    assert event.data["to"] == str(org.b_mad.pk)


def test_a_stranded_request_moves_and_one_in_place_stays_quiet(
    org: Org, people: People, make_request: MakeRequest
) -> None:
    """A request an earlier change left on another branch moves; one already on the new
    branch is left alone, without an audit event."""
    stranded = make_request(Status.APPROVED, decided=True)
    in_place = make_request(Status.SUBMITTED)
    MaterialRequest.objects.filter(pk=stranded.pk).update(procurement_branch=org.a_sev)
    MaterialRequest.objects.filter(pk=in_place.pk).update(procurement_branch=org.b_mad)

    _move(people, org.hospital, org.b_mad.pk)

    assert {_branch_of(stranded), _branch_of(in_place)} == {org.b_mad.pk}
    moved = AuditEvent.objects.filter(action="request.procurement_branch_changed")
    assert [(event.object_id, event.data["from"]) for event in moved] == [
        (stranded.pk, str(org.a_sev.pk))
    ]


def test_other_projects_keep_their_requests(
    org: Org, people: People, make_request: MakeRequest
) -> None:
    other = make_request(Status.APPROVED, project=org.sev_for_mad, decided=True)
    _move(people, org.hospital, org.b_mad.pk)
    assert _branch_of(other) == org.a_mad.pk


@pytest.mark.parametrize(
    "flow",
    [
        ledger.Flows(allocated=Decimal("1"), reserved=Decimal("1")),
        ledger.Flows(received=Decimal("1")),
        ledger.Flows(delivered=Decimal("1")),
    ],
)
def test_a_committed_request_refuses_the_change(
    org: Org,
    people: People,
    make_request: MakeRequest,
    flows_of: dict[UUID, ledger.Flows],
    flow: ledger.Flows,
) -> None:
    committed = make_request(Status.APPROVED, decided=True)
    free = make_request(Status.SUBMITTED)
    flows_of[committed.lines.get().pk] = flow

    with pytest.raises(ValidationError) as excinfo:
        _move(people, org.hospital, org.b_mad.pk)

    assert excinfo.value.message_dict.keys() == {"procurement_branch"}
    assert str(committed.number) in excinfo.value.message_dict["procurement_branch"][0]
    assert str(free.number) not in excinfo.value.message_dict["procurement_branch"][0]
    org.hospital.refresh_from_db()
    assert org.hospital.procurement_branch_id == org.a_mad.pk
    assert {_branch_of(committed), _branch_of(free)} == {org.a_mad.pk}
    assert not AuditEvent.objects.filter(action="request.procurement_branch_changed").exists()


def test_other_project_changes_leave_requests_alone(
    org: Org, people: People, make_request: MakeRequest
) -> None:
    request = make_request(Status.APPROVED, decided=True)
    services.update_project(principal_for(people.group_admin), org.hospital.pk, name="Renamed")
    assert _branch_of(request) == org.a_mad.pk


def test_the_registry_registers_once_and_unregisters(org: Org, people: People) -> None:
    calls: list[procurement.ProcurementChange] = []
    unregister = procurement.register_follower(calls.append)
    procurement.register_follower(calls.append)
    try:
        _move(people, org.hospital, org.b_mad.pk)
    finally:
        unregister()
        unregister()
    assert [(c.old_branch_id, c.new_branch_id) for c in calls] == [(org.a_mad.pk, org.b_mad.pk)]
    _move(people, org.hospital, org.a_mad.pk)
    assert len(calls) == 1
