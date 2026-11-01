"""Fixtures of the purchasing tests: materials, approved request lines, orders built through
the services, and stand-ins for what receipts (M6.1) and handovers (M6.2) will contribute.

Receipts are faked per order line (received, posted) and per allocation (attributed); a
direct receipt at a worksite also delivers (D), which ``delivered`` fakes per request line.
"""

from collections.abc import Callable, Iterator
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from django.utils import timezone

from apps.access.principals import SessionPrincipal
from apps.catalog.models import Material, UnitOfMeasure
from apps.material_requests import ledger
from apps.material_requests.models import MaterialRequest, MaterialRequestLine
from apps.projects.models import Project
from apps.purchasing import receipts, services
from apps.purchasing.models import PurchaseAllocation, PurchaseOrder, PurchaseOrderLine
from apps.purchasing.receipts import LineReceipts
from apps.purchasing.services import RequestItem
from tests.org import Org, People
from tests.support import principal_for

D = Decimal
ZERO = Decimal("0")


@pytest.fixture
def cable(db: None) -> Material:
    """Metres, two decimals."""
    return Material.objects.create(
        reference="CAB-25", name="Cable 2,5", unit=UnitOfMeasure.objects.get(code="M")
    )


@pytest.fixture
def breaker(db: None) -> Material:
    """Units, no decimals."""
    return Material.objects.create(
        reference="DIF-40", name="Diferencial 40A", unit=UnitOfMeasure.objects.get(code="UD")
    )


@pytest.fixture
def mm(people: People) -> SessionPrincipal:
    """A-MAD's material manager, with session MFA: buys for A-MAD and B-MAD."""
    return principal_for(people.mm_mad)


type MakeLine = Callable[..., MaterialRequestLine]


@pytest.fixture
def make_line(org: Org, people: People, cable: Material) -> MakeLine:
    """An approved request line to purchase (one per request), written directly.

    Requests are submitted a minute apart in creation order, so with equal priority and
    needed-by dates the attribution order is the creation order.
    """
    count = iter(range(1, 10_000))
    start = timezone.now() - timedelta(days=1)

    def make(
        approved: str = "10",
        *,
        project: Project | None = None,
        material: Material | None = None,
        priority: str = MaterialRequest.Priority.NORMAL,
        needed_by: date | None = None,
        submitted_at: datetime | None = None,
        sourcing: str = MaterialRequestLine.Sourcing.PURCHASE,
        status: str = MaterialRequest.Status.APPROVED,
        requested: str | None = None,
    ) -> MaterialRequestLine:
        number = next(count)
        project = project or org.hospital
        material = material or cable
        now = timezone.now()
        closed = {"closed_at": now, "close_mode": "AUTO"} if status == "CLOSED" else {}
        request = MaterialRequest.objects.create(
            project=project,
            procurement_branch_id=project.procurement_branch_id,
            requested_by=people.tech_mad,
            status=status,
            number=f"SOL-T-{number:05d}",
            submitted_at=submitted_at or start + timedelta(minutes=number),
            decided_at=now,
            priority=priority,
            needed_by=needed_by,
            **closed,
        )
        return MaterialRequestLine.objects.create(
            request=request,
            project=project,
            line_no=1,
            material=material,
            unit_id=material.unit_id,
            requested_qty=D(requested or approved),
            approved_qty=D(approved),
            decision=MaterialRequestLine.Decision.APPROVED,
            decided_by=people.mm_mad,
            decided_at=now,
            sourcing=sourcing,
        )

    return make


class FakeReceipts:
    """Stands in for receiving (M6.1): ``receive`` posts a receipt on an order line."""

    def __init__(self) -> None:
        self.received: dict[UUID, Decimal] = {}
        self.by_allocation: dict[UUID, Decimal] = {}

    def receive(
        self,
        po_line: PurchaseOrderLine,
        qty: str,
        attributions: dict[PurchaseAllocation, str] | None = None,
    ) -> None:
        self.received[po_line.pk] = self.received.get(po_line.pk, ZERO) + D(qty)
        for allocation, part in (attributions or {}).items():
            self.by_allocation[allocation.pk] = self.by_allocation.get(allocation.pk, ZERO) + D(
                part
            )

    def lines(self, ids: Any) -> dict[UUID, LineReceipts]:
        return {i: LineReceipts(self.received[i], True) for i in ids if i in self.received}

    def attributed(self, ids: Any) -> dict[UUID, Decimal]:
        return {i: self.by_allocation[i] for i in ids if i in self.by_allocation}


@pytest.fixture
def received() -> Iterator[FakeReceipts]:
    source = FakeReceipts()
    unregister = receipts.register_receipt_source(source)
    yield source
    unregister()


@pytest.fixture
def delivered() -> Iterator[dict[UUID, Decimal]]:
    """D per request line, as handovers (M6.2) will contribute it."""
    table: dict[UUID, Decimal] = {}
    unregister = ledger.register_flow_source(
        lambda ids: {i: ledger.Flows(delivered=table[i]) for i in ids if i in table}
    )
    yield table
    unregister()


type Buy = Callable[..., PurchaseOrder]


@pytest.fixture
def buy(org: Org, mm: SessionPrincipal) -> Buy:
    """An order of A-MAD through the services: ``buy((line, qty), …, issue=True)``."""

    def make(
        *items: tuple[MaterialRequestLine, str], issue: bool = True, ship_to: Any = None
    ) -> PurchaseOrder:
        order = services.create_po(
            mm,
            branch_id=org.a_mad.pk,
            supplier_id=org.supplier.pk,
            ship_to_id=(ship_to or org.warehouses["A Madrid"]).pk,
        )
        if items:
            services.add_request_lines(
                mm, order.pk, [RequestItem(line.pk, D(qty)) for line, qty in items]
            )
        if issue:
            services.issue(mm, order.pk)
        return PurchaseOrder.objects.get(pk=order.pk)

    return make


def q(*values: int | str) -> tuple[Decimal, ...]:
    """Expected quantities as ``Decimal`` (the ledger never mixes in ints or floats)."""
    return tuple(Decimal(value) for value in values)


def allocation(order: PurchaseOrder, line: MaterialRequestLine) -> PurchaseAllocation:
    return PurchaseAllocation.objects.get(po_line__po=order, request_line=line)


def only_line(order: PurchaseOrder) -> PurchaseOrderLine:
    return order.lines.get()


def flows(line: MaterialRequestLine) -> ledger.Flows:
    return ledger.flows([line.pk])[line.pk]


def ledger_of(line: MaterialRequestLine) -> ledger.LineLedger:
    (entry,) = ledger.line_ledgers(MaterialRequestLine.objects.filter(pk=line.pk))
    return entry
