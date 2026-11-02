"""Fixtures of the receiving tests: the purchasing fixtures (approved request lines, orders
bought through the services) and ``receive``, which posts a receipt through the service.

A-MAD buys for three active projects here: the hospital (branch B-MAD), SEV-2 (branch
A-SEV) and ``third`` (MAD-2, branch A-MAD), each with its worksite.
"""

from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest

from apps.access.principals import SessionPrincipal
from apps.locations.models import Location
from apps.projects.models import Project
from apps.purchasing.models import PurchaseOrder, PurchaseOrderLine
from apps.receiving import services
from apps.receiving.models import GoodsReceipt
from apps.receiving.services import ReceiptLineInput
from tests.org import Org
from tests.unit.purchasing.conftest import (  # noqa: F401 - fixtures used by these tests
    breaker,
    buy,
    cable,
    make_line,
    mm,
)

D = Decimal


@pytest.fixture
def third(org: Org) -> Project:
    """MAD-2: a project of A-MAD, bought by A-MAD, with its worksite."""
    project = Project.objects.create(
        code="MAD-2",
        name="MAD-2",
        branch=org.a_mad,
        procurement_branch=org.a_mad,
        status=Project.Status.ACTIVE,
    )
    Location.objects.create(
        kind=Location.Kind.WORKSITE, branch=org.a_mad, project=project, name="site MAD-2"
    )
    return project


type Line = PurchaseOrderLine | str | ReceiptLineInput | tuple[PurchaseOrderLine, str]
type Receive = Callable[..., GoodsReceipt]


def as_input(order: PurchaseOrder, line: Line) -> ReceiptLineInput:
    """``"6"`` receives 6 on the order's only line; ``(line, "6")`` on that line."""
    if isinstance(line, ReceiptLineInput):
        return line
    if isinstance(line, str):
        return ReceiptLineInput(order.lines.get().pk, D(line))
    if isinstance(line, tuple):
        return ReceiptLineInput(line[0].pk, D(line[1]))
    raise TypeError(line)


@pytest.fixture
def receive(org: Org, mm: SessionPrincipal) -> Receive:  # noqa: F811 - the fixture above
    """``receive(order, "6", at=worksite)``: a receipt through the service (A-MAD's
    warehouse and its material manager by default)."""

    def make(
        order: PurchaseOrder,
        *lines: Line,
        at: Location | None = None,
        principal: SessionPrincipal | None = None,
        **kwargs: Any,
    ) -> GoodsReceipt:
        return services.post_receipt(
            principal or mm,
            order.pk,
            location_id=(at or org.warehouses["A Madrid"]).pk,
            lines=[as_input(order, line) for line in lines],
            **kwargs,
        )

    return make
