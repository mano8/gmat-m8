"""The access matrix of the purchasing services (plan §3.4, S5 to S9, §8.2).

Every operation, called directly as every actor of the shared fixture, on orders of A-MAD
(which buys for the hospital): ``po.manage`` belongs to group administrators and A-MAD's
material manager. Actors who see the order (``po.view``) without it get 403
(``AccessDenied``): the warehouse operator, and a deactivated account before any scope;
everyone else gets 404 (``NotInScope``). The screens of M5.2 add their URL rows to
``tests/access/test_acceptance_matrix.py``; M5.3 adds the decisions on a dispatch.
"""

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest

from apps.access.exceptions import AccessDenied, MFARequired, NotInScope
from apps.catalog.models import Material
from apps.material_requests.models import MaterialRequestLine
from apps.notifications.models import EmailOutbox
from apps.notifications.models import Status as MailStatus
from apps.organizations.models import Branch
from apps.purchasing import services
from apps.purchasing.models import PurchaseAllocation, PurchaseOrder, PurchaseOrderLine
from apps.purchasing.services import RequestItem
from tests import orders
from tests.org import Org, People
from tests.outbox import make_dispatch
from tests.support import principal_for
from tests.unit.purchasing.conftest import MakeLine

pytestmark = pytest.mark.django_db

D = Decimal


@dataclass
class State:
    """A draft (a line of 12: 5 allocated to one request line, 5 to ``spare``), an issued
    order (10 for 10) and another draft with a free line, all of one branch; ``spare`` has
    5 left to purchase."""

    branch: Branch
    draft: PurchaseOrder
    draft_line: PurchaseOrderLine
    draft_allocation: PurchaseAllocation
    issued: PurchaseOrder
    issued_line: PurchaseOrderLine
    spare: MaterialRequestLine
    free_line: PurchaseOrderLine  # on another draft, nothing allocated
    dispatch: EmailOutbox  # the issued order's, left AMBIGUOUS by the worker (M5.3)


def build(
    org: Org, people: People, make_line: MakeLine, cable: Material, *, sevilla: bool
) -> State:
    branch = org.a_sev if sevilla else org.a_mad
    project = org.sev_works if sevilla else org.hospital
    issuer = people.mm_sev if sevilla else people.mm_mad
    first, second, spare = (make_line("10", project=project) for _ in range(3))
    draft = orders.order(org, branch=branch)
    draft_line = orders.line(draft, cable, "12")
    draft_allocation = orders.allocate(draft_line, first, "5")
    issued = orders.order(org, branch=branch, status="ISSUED", issued_by=issuer)
    issued_line = orders.line(issued, cable, "10")
    orders.allocate(issued_line, second, "10")
    orders.allocate(draft_line, spare, "5")  # spare: 5 reserved, 5 left
    free_line = orders.line(orders.order(org, branch=branch), cable, "5")
    dispatch = make_dispatch(issued, MailStatus.AMBIGUOUS)
    return State(
        branch,
        draft,
        draft_line,
        draft_allocation,
        issued,
        issued_line,
        spare,
        free_line,
        dispatch,
    )


type Operation = Callable[[Any, State, Org], Any]

OPERATIONS: dict[str, Operation] = {
    "create_po": lambda p, s, org: services.create_po(
        p,
        branch_id=s.branch.pk,
        supplier_id=org.supplier.pk,
        ship_to_id=org.warehouses[s.branch.name].pk,
    ),
    "update_order": lambda p, s, org: services.update_order(p, s.draft.pk, notes="Call first"),
    "add_request_lines": lambda p, s, org: services.add_request_lines(
        p, s.draft.pk, [RequestItem(s.spare.pk, D(1))]
    ),
    "allocate": lambda p, s, org: services.allocate(
        p, s.free_line.pk, request_line_id=s.spare.pk, qty=D(1)
    ),
    "increase_allocation": lambda p, s, org: services.increase_allocation(
        p, s.draft_allocation.pk, qty=D("5.5")
    ),
    "unallocate": lambda p, s, org: services.unallocate(p, s.draft_allocation.pk),
    "update_line": lambda p, s, org: services.update_line(p, s.draft_line.pk, ordered_qty=D(11)),
    "update_line_price": lambda p, s, org: services.update_line(
        p, s.draft_line.pk, unit_price=D("1.5")
    ),
    "remove_line": lambda p, s, org: services.remove_line(p, s.draft_line.pk),
    "issue": lambda p, s, org: services.issue(p, s.draft.pk),
    "amend": lambda p, s, org: services.amend(
        p, s.issued.pk, reason="Call", header=services.HeaderAmendment(notes="x")
    ),
    "cancel": lambda p, s, org: services.cancel(p, s.issued.pk, reason="Not needed"),
    "close_line_short": lambda p, s, org: services.close_line_short(
        p, s.issued_line.pk, reason="Short"
    ),
    "confirm_dispatch": lambda p, s, org: services.confirm_dispatch(p, s.dispatch.pk),
    "resend_dispatch": lambda p, s, org: services.resend_dispatch(p, s.dispatch.pk),
}

ALLOWED = {"group_admin", "mm_mad"}
# Sees A-MAD's orders (po.view) without po.manage, or is refused before any scope: 403.
DENIED = {"wo_mad", "deactivated"}
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


def expected(actor: str) -> type[Exception] | None:
    if actor in ALLOWED:
        return None
    return AccessDenied if actor in DENIED else NotInScope


def snapshot(state: State) -> tuple[object, ...]:
    return (
        PurchaseOrder.objects.count(),
        sorted(PurchaseOrder.objects.values_list("status", "revision", "notes")),
        sorted(PurchaseOrderLine.objects.values_list("ordered_qty", "unit_price")),
        sorted(PurchaseAllocation.objects.values_list("qty", flat=True)),
        sorted(EmailOutbox.objects.values_list("status", flat=True)),
    )


@pytest.fixture
def state(org: Org, people: People, make_line: MakeLine, cable: Material) -> State:
    return build(org, people, make_line, cable, sevilla=False)


@pytest.mark.parametrize("actor", ACTORS)
@pytest.mark.parametrize("operation", sorted(OPERATIONS))
def test_matrix(state: State, org: Org, people: People, operation: str, actor: str) -> None:
    call = OPERATIONS[operation]
    principal = principal_for(getattr(people, actor))
    error = expected(actor)
    if error is None:
        call(principal, state, org)
        return
    before = snapshot(state)
    with pytest.raises(error):
        call(principal, state, org)
    assert snapshot(state) == before


@pytest.mark.parametrize("operation", sorted(OPERATIONS))
def test_privileged_buyers_need_session_mfa(
    org: Org, people: People, make_line: MakeLine, cable: Material, operation: str
) -> None:
    for user in (people.group_admin, people.mm_mad):
        state = build(org, people, make_line, cable, sevilla=False)
        with pytest.raises(MFARequired):
            OPERATIONS[operation](principal_for(user, mfa_at=None), state, org)


@pytest.mark.parametrize("operation", sorted(OPERATIONS))
def test_capabilities_never_mix_across_memberships(
    org: Org, people: People, make_line: MakeLine, cable: Material, operation: str
) -> None:
    """The mixed user manages A-SEV's materials and is a technician in A-MAD (S6)."""
    call = OPERATIONS[operation]
    call(principal_for(people.mixed), build(org, people, make_line, cable, sevilla=True), org)
    call(principal_for(people.mm_sev), build(org, people, make_line, cable, sevilla=True), org)
    with pytest.raises(NotInScope):
        call(principal_for(people.mm_sev), build(org, people, make_line, cable, sevilla=False), org)
