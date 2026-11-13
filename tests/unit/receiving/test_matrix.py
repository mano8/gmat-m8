"""The access matrix of the receiving services (plan §3.4, Q9, S5 to S9, §8.2).

Every operation, called directly as every actor of the shared fixture, on an issued order
of A-MAD that buys for the hospital:

- ``post_at_warehouse`` (A-MAD's warehouse): ``receipt.post`` on the order: group
  administrators, A-MAD's material manager, engineer and warehouse operator. Who receives
  only at worksites (○ site) does not see the order: 404.
- ``post_at_site`` (the hospital's worksite): the same, plus everyone whose membership's
  scope holds the hospital (Q44, ADR-0028): its site manager and partner, A-MAD's
  technician (B-MAD, the hospital's branch, is managed by A-MAD), the mixed user through
  their technician membership; whoever requested the goods.
- ``void`` (a receipt at the hospital's worksite): ``receipt.void`` belongs to group
  administrators, material managers and engineers. Who sees the receipt without it gets
  403: the warehouse operator, the site manager, the technician, the partner.

A deactivated account is refused before any scope (403); everyone else gets 404. M6.3 adds
the URL rows to ``tests/access/test_acceptance_matrix.py``.
"""

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest

from apps.access.exceptions import AccessDenied, MFARequired, NotInScope
from apps.handovers.models import Handover
from apps.organizations.models import Branch
from apps.projects.models import Project
from apps.purchasing import services as purchasing
from apps.purchasing.models import PurchaseOrder
from apps.purchasing.services import RequestItem
from apps.receiving import services
from apps.receiving.models import GoodsReceipt, ReceiptAttribution
from apps.receiving.services import ReceiptLineInput
from tests.org import Org, People
from tests.support import principal_for
from tests.unit.purchasing.conftest import MakeLine, only_line

pytestmark = pytest.mark.django_db

D = Decimal


@dataclass
class State:
    """An issued order of 20 for a request line of the project (10 + 10 spare to receive)
    and a receipt of 2 posted at the project's worksite."""

    branch: Branch
    project: Project
    order: PurchaseOrder
    receipt: GoodsReceipt


def build(org: Org, people: People, make_line: MakeLine, *, sevilla: bool) -> State:
    branch = org.a_sev if sevilla else org.a_mad
    project = org.sev_works if sevilla else org.hospital
    buyer = principal_for(people.mm_sev if sevilla else people.mm_mad)
    first, second = make_line("10", project=project), make_line("10", project=project)
    order = purchasing.create_po(
        buyer,
        branch_id=branch.pk,
        supplier_id=org.supplier.pk,
        ship_to_id=org.warehouses[branch.name].pk,
    )
    purchasing.add_request_lines(
        buyer, order.pk, [RequestItem(first.pk, D(10)), RequestItem(second.pk, D(10))]
    )
    purchasing.issue(buyer, order.pk)
    receipt = services.post_receipt(
        buyer,
        order.pk,
        location_id=org.worksite(project).pk,
        lines=[ReceiptLineInput(only_line(order).pk, D(2))],
    )
    return State(branch, project, PurchaseOrder.objects.get(pk=order.pk), receipt)


def _post(principal: Any, state: State, org: Org, *, site: bool) -> GoodsReceipt:
    location = org.worksite(state.project) if site else org.warehouses[state.branch.name]
    return services.post_receipt(
        principal,
        state.order.pk,
        location_id=location.pk,
        lines=[ReceiptLineInput(only_line(state.order).pk, D(1))],
    )


type Operation = Callable[[Any, State, Org], Any]

OPERATIONS: dict[str, Operation] = {
    "post_at_warehouse": lambda p, s, org: _post(p, s, org, site=False),
    "post_at_site": lambda p, s, org: _post(p, s, org, site=True),
    "void": lambda p, s, org: services.void_receipt(p, s.receipt.pk, reason="Wrong order"),
}

BRANCH = {"group_admin", "mm_mad", "engineer", "wo_mad"}
SITE = {"site_manager", "tech_mad", "partner", "mixed"}
ALLOWED = {
    "post_at_warehouse": BRANCH,
    "post_at_site": BRANCH | SITE,
    "void": {"group_admin", "mm_mad", "engineer"},
}
# Refused before any scope (deactivated), or sees the receipt without receipt.void: 403.
DENIED = {
    "post_at_warehouse": {"deactivated"},
    "post_at_site": {"deactivated"},
    "void": {"deactivated", "wo_mad"} | SITE,
}
ACTORS = [
    "group_admin",
    "operator",
    "mm_mad",
    "mm_sev",
    "engineer",
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
        sorted(GoodsReceipt.objects.values_list("status", flat=True)),
        ReceiptAttribution.objects.count(),
        sorted(Handover.objects.values_list("status", flat=True)),
    )


@pytest.fixture
def state(org: Org, people: People, make_line: MakeLine) -> State:
    return build(org, people, make_line, sevilla=False)


@pytest.mark.parametrize("actor", ACTORS)
@pytest.mark.parametrize("operation", sorted(OPERATIONS))
def test_matrix(state: State, org: Org, people: People, operation: str, actor: str) -> None:
    call = OPERATIONS[operation]
    principal = principal_for(getattr(people, actor))
    error = expected(operation, actor)
    if error is None:
        call(principal, state, org)
        return
    before = snapshot()
    with pytest.raises(error):
        call(principal, state, org)
    assert snapshot() == before


@pytest.mark.parametrize("operation", sorted(OPERATIONS))
def test_privileged_roles_need_session_mfa(
    org: Org, people: People, make_line: MakeLine, operation: str
) -> None:
    state = build(org, people, make_line, sevilla=False)
    for user in (people.group_admin, people.mm_mad, people.engineer):
        with pytest.raises(MFARequired):
            OPERATIONS[operation](principal_for(user, mfa_at=None), state, org)


@pytest.mark.parametrize("operation", ["post_at_warehouse", "post_at_site"])
def test_ordinary_roles_need_no_mfa(
    org: Org, people: People, make_line: MakeLine, operation: str
) -> None:
    state = build(org, people, make_line, sevilla=False)
    OPERATIONS[operation](principal_for(people.wo_mad, mfa_at=None), state, org)


@pytest.mark.parametrize("actor", ["site_manager", "tech_mad", "partner"])
def test_site_receivers_post_without_mfa(
    org: Org, people: People, make_line: MakeLine, actor: str
) -> None:
    state = build(org, people, make_line, sevilla=False)
    OPERATIONS["post_at_site"](principal_for(getattr(people, actor), mfa_at=None), state, org)


# What the mixed user's technician membership in A-MAD gives on A-MAD's order (S6): the
# material manager's membership of A-SEV covers none of it.
MIXED_IN_MADRID: dict[str, type[Exception] | None] = {
    "post_at_warehouse": NotInScope,
    "post_at_site": None,  # ○ site: the hospital is a project of the technician's scope
    "void": AccessDenied,  # they see the site receipt, but void nothing in A-MAD
}


@pytest.mark.parametrize("operation", sorted(OPERATIONS))
def test_capabilities_never_mix_across_memberships(
    org: Org, people: People, make_line: MakeLine, operation: str
) -> None:
    """The mixed user manages A-SEV's materials and is a technician in A-MAD (S6)."""
    call = OPERATIONS[operation]
    call(principal_for(people.mixed), build(org, people, make_line, sevilla=True), org)
    madrid = build(org, people, make_line, sevilla=False)
    error = MIXED_IN_MADRID[operation]
    if error is None:
        # The technician's grant covers it alone: no session MFA, which only the material
        # manager's privileged grant would ask for.
        call(principal_for(people.mixed, mfa_at=None), madrid, org)
        return
    with pytest.raises(error):
        call(principal_for(people.mixed), madrid, org)
