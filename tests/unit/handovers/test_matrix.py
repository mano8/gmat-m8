"""The access matrix of the handover services (plan §3.4, S5 to S9, §8.2).

Every operation, called directly as every actor of the shared fixture, on an approved
request of the hospital (bought by A-MAD):

- ``post`` (from A-MAD's warehouse): ``handover.post`` on the requests served, whose
  procurement branch is the handover's (C8): group administrators, A-MAD's material
  manager and warehouse operator. The hospital's site manager sees the request through
  ``handover.view`` (○ assigned) but may not post: 403.
- ``void`` (a ``FROM_WAREHOUSE`` handover of the hospital): ``handover.void`` belongs to
  group administrators and material managers. Who sees the handover without it gets 403:
  the warehouse operator and the site manager.

A deactivated account is refused before any scope (403); everyone else gets 404. M6.3 adds
the URL rows to ``tests/access/test_acceptance_matrix.py``.
"""

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest

from apps.access.exceptions import AccessDenied, MFARequired, NotInScope
from apps.handovers import services
from apps.handovers.models import Handover, HandoverLine
from apps.handovers.services import HandoverLineInput, Receiver
from apps.locations.models import Location
from apps.material_requests.models import MaterialRequestLine
from apps.projects.models import Project
from tests.org import Org, People
from tests.support import principal_for
from tests.unit.purchasing.conftest import MakeLine

pytestmark = pytest.mark.django_db

D = Decimal
CREW = Receiver(name="Site crew")


@dataclass
class State:
    """A request line of 10 from stock, with a handover of 2 already posted."""

    project: Project
    warehouse: Location
    line: MaterialRequestLine
    handover: Handover


def build(org: Org, people: People, make_line: MakeLine, *, sevilla: bool) -> State:
    project = org.sev_works if sevilla else org.hospital
    warehouse = org.warehouses["A Sevilla" if sevilla else "A Madrid"]
    line = make_line("10", project=project, sourcing=MaterialRequestLine.Sourcing.STOCK)
    handover = services.create_handover(
        principal_for(people.mm_sev if sevilla else people.mm_mad),
        project.pk,
        source_location_id=warehouse.pk,
        lines=[HandoverLineInput(line.pk, D(2))],
        receiver=CREW,
    )
    return State(project, warehouse, line, handover)


def _post(principal: Any, state: State) -> Handover:
    return services.create_handover(
        principal,
        state.project.pk,
        source_location_id=state.warehouse.pk,
        lines=[HandoverLineInput(state.line.pk, D(1))],
        receiver=CREW,
    )


type Operation = Callable[[Any, State], Any]

OPERATIONS: dict[str, Operation] = {
    "post": _post,
    "void": lambda p, s: services.void_handover(p, s.handover.pk, reason="Wrong person"),
}

ALLOWED = {
    "post": {"group_admin", "mm_mad", "wo_mad"},
    "void": {"group_admin", "mm_mad"},
}
# Refused before any scope (deactivated), or sees the request or handover without the
# capability: 403.
DENIED = {
    "post": {"deactivated", "site_manager"},
    "void": {"deactivated", "wo_mad", "site_manager"},
}
ACTORS = [
    "group_admin",
    "operator",
    "mm_mad",
    "mm_sev",
    "wo_mad",
    "tech_mad",
    "site_manager",
    "partner",
    "mixed",
    "expired",
    "revoked",
    "revoked_assignment",
    "inactive_partner",
    "inactive_branch",
    "deactivated",
    "nobody",
]


def expected(operation: str, actor: str) -> type[Exception] | None:
    if actor in ALLOWED[operation]:
        return None
    return AccessDenied if actor in DENIED[operation] else NotInScope


def snapshot() -> tuple[object, ...]:
    return (
        sorted(Handover.objects.values_list("status", flat=True)),
        HandoverLine.objects.count(),
    )


@pytest.fixture
def state(org: Org, people: People, make_line: MakeLine) -> State:
    return build(org, people, make_line, sevilla=False)


@pytest.mark.parametrize("actor", ACTORS)
@pytest.mark.parametrize("operation", sorted(OPERATIONS))
def test_matrix(state: State, people: People, operation: str, actor: str) -> None:
    call = OPERATIONS[operation]
    principal = principal_for(getattr(people, actor))
    error = expected(operation, actor)
    if error is None:
        call(principal, state)
        return
    before = snapshot()
    with pytest.raises(error):
        call(principal, state)
    assert snapshot() == before


@pytest.mark.parametrize("operation", sorted(OPERATIONS))
def test_privileged_roles_need_session_mfa(
    org: Org, people: People, make_line: MakeLine, operation: str
) -> None:
    state = build(org, people, make_line, sevilla=False)
    for user in (people.group_admin, people.mm_mad):
        with pytest.raises(MFARequired):
            OPERATIONS[operation](principal_for(user, mfa_at=None), state)


def test_warehouse_operators_post_without_mfa(state: State, people: People) -> None:
    OPERATIONS["post"](principal_for(people.wo_mad, mfa_at=None), state)


@pytest.mark.parametrize("operation", sorted(OPERATIONS))
def test_capabilities_never_mix_across_memberships(
    org: Org, people: People, make_line: MakeLine, operation: str
) -> None:
    """The mixed user manages A-SEV's materials and is a technician in A-MAD (S6)."""
    call = OPERATIONS[operation]
    call(principal_for(people.mixed), build(org, people, make_line, sevilla=True))
    with pytest.raises(NotInScope):
        call(principal_for(people.mixed), build(org, people, make_line, sevilla=False))
