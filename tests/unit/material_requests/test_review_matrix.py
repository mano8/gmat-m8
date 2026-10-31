"""The access matrix of the review and sourcing services (plan §3.4, S5 to S9, §8.2).

Every operation, called directly as every actor of the shared fixture, on a hospital request
(project branch B-MAD, procurement branch A-MAD) made by A-MAD's technician:

- ``request.review``: group administrators, A-MAD's material manager and the hospital's
  site manager (assigned);
- ``request.source``: group administrators and A-MAD's material manager.

Actors who can see the request but lack the capability get 403 (``AccessDenied``); actors
who cannot see it get 404 (``NotInScope``). The screens of M4.4 add their URL rows to
``tests/access/test_acceptance_matrix.py``.
"""

from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest

from apps.access.exceptions import AccessDenied, MFARequired, NotInScope
from apps.catalog.models import Material, UnitOfMeasure
from apps.material_requests import services
from apps.material_requests.models import MaterialRequest, MaterialRequestLine
from tests.org import Org, People
from tests.support import principal_for
from tests.unit.material_requests.conftest import MakeRequest

pytestmark = pytest.mark.django_db

Status = MaterialRequest.Status

type Operation = Callable[[Any, MaterialRequest, Material], Any]


def _line(request: MaterialRequest, line_no: int = 1) -> Any:
    return MaterialRequestLine.objects.get(request=request, line_no=line_no).pk


def _closed_line(request: MaterialRequest) -> Any:
    MaterialRequestLine.objects.filter(request=request, line_no=1).update(
        closed_short_at=request.created_at,
        closed_short_by=request.requested_by,
        closed_short_source="LINE",
        close_reason="x",
    )
    return _line(request)


# operation → (capability, state of the request it acts on, call)
OPERATIONS: dict[str, tuple[str, str, Operation]] = {
    "decide_line": (
        "review",
        Status.SUBMITTED,
        lambda p, r, m: services.decide_line(p, _line(r), decision="APPROVED"),
    ),
    "approve_all": ("review", Status.SUBMITTED, lambda p, r, m: services.approve_all(p, r.pk)),
    "return_to_draft": (
        "review",
        Status.APPROVED,
        lambda p, r, m: services.return_to_draft(p, r.pk, note="x"),
    ),
    "reduce_approved_qty": (
        "review",
        Status.APPROVED,
        lambda p, r, m: services.reduce_approved_qty(p, _line(r), approved_qty=Decimal(1)),
    ),
    "set_sourcing": (
        "source",
        Status.APPROVED,
        lambda p, r, m: services.set_sourcing(p, _line(r), sourcing="PURCHASE"),
    ),
    "resolve_non_catalog_line": (
        "source",
        Status.APPROVED,
        lambda p, r, m: services.resolve_non_catalog_line(p, _line(r, 2), material_id=m.pk),
    ),
    "change_priority": (
        "source",
        Status.APPROVED,
        lambda p, r, m: services.change_priority(p, r.pk, priority="URGENT"),
    ),
    "change_needed_by": (
        "source",
        Status.APPROVED,
        lambda p, r, m: services.change_needed_by(p, r.pk, needed_by=None),
    ),
    "close_line_short": (
        "source",
        Status.APPROVED,
        lambda p, r, m: services.close_line_short(p, _line(r), reason="x"),
    ),
    "reopen_line": (
        "source",
        Status.APPROVED,
        lambda p, r, m: services.reopen_line(p, _closed_line(r), reason="x"),
    ),
    "manual_close": (
        "source",
        Status.APPROVED,
        lambda p, r, m: services.manual_close(p, r.pk, reason="x"),
    ),
    "reopen": ("source", Status.CLOSED, lambda p, r, m: services.reopen(p, r.pk, reason="x")),
}

ALLOWED = {
    "review": {"group_admin", "mm_mad", "site_manager"},
    "source": {"group_admin", "mm_mad"},
}
# Can see the request (request.view) without the capability: 403.
SEES = {"group_admin", "mm_mad", "site_manager", "wo_mad", "tech_mad", "deactivated"}
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


def expected(capability: str, actor: str) -> type[Exception] | None:
    if actor in ALLOWED[capability]:
        return None
    return AccessDenied if actor in SEES else NotInScope


@pytest.fixture
def setup(make_request: MakeRequest, cable: Material) -> Callable[..., MaterialRequest]:
    """A request in ``state`` with a catalog line and a non-catalog line (metres)."""

    def make(state: str, **kwargs: Any) -> MaterialRequest:
        request = make_request(state, decided=state != Status.SUBMITTED, **kwargs)
        decided = request.lines.get()
        MaterialRequestLine.objects.create(
            request=request,
            project_id=request.project_id,
            line_no=2,
            free_text="Bandeja 300",
            unit=UnitOfMeasure.objects.get(code="M"),
            requested_qty=Decimal(4),
            decision=decided.decision,
            approved_qty=decided.approved_qty and Decimal(4),
            decided_by=decided.decided_by,
            decided_at=decided.decided_at,
        )
        return request

    return make


@pytest.mark.parametrize("actor", ACTORS)
@pytest.mark.parametrize("operation", sorted(OPERATIONS))
def test_matrix(
    setup: Callable[..., MaterialRequest],
    people: People,
    cable: Material,
    operation: str,
    actor: str,
) -> None:
    capability, state, call = OPERATIONS[operation]
    request = setup(state)
    principal = principal_for(getattr(people, actor))
    error = expected(capability, actor)
    if error is None:
        call(principal, request, cable)
        return
    with pytest.raises(error):
        call(principal, request, cable)
    after = MaterialRequest.objects.get(pk=request.pk)
    assert after.status == state
    assert after.priority == "NORMAL"


@pytest.mark.parametrize("operation", sorted(OPERATIONS))
def test_privileged_reviewers_need_session_mfa(
    setup: Callable[..., MaterialRequest], people: People, cable: Material, operation: str
) -> None:
    capability, state, call = OPERATIONS[operation]
    for user in (people.group_admin, people.mm_mad):
        with pytest.raises(MFARequired):
            call(principal_for(user, mfa_at=None), setup(state), cable)
    if capability == "review":  # site managers are not privileged (§3.6)
        call(principal_for(people.site_manager, mfa_at=None), setup(state), cable)


@pytest.mark.parametrize("operation", sorted(OPERATIONS))
def test_capabilities_never_mix_across_memberships(
    setup: Callable[..., MaterialRequest],
    org: Org,
    people: People,
    cable: Material,
    operation: str,
) -> None:
    """The mixed user manages A-SEV's materials and is a technician in A-MAD (S6)."""
    _capability, state, call = OPERATIONS[operation]
    call(principal_for(people.mixed), setup(state, project=org.sev_works), cable)
    call(principal_for(people.mm_sev), setup(state, project=org.sev_works), cable)
    with pytest.raises(NotInScope):  # A-MAD buys for SEV-2: not A-SEV's to review (S5)
        call(principal_for(people.mm_sev), setup(state, project=org.sev_for_mad), cable)
