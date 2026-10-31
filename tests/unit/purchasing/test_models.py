"""Purchase-order models: constraints, the C9 composite key and the state machine.

Rows are written with the ORM, bypassing the services: the database refuses what the
services would never write (plan §4.6: C9 by a composite foreign key; quantities, prices,
closures and issue fields by CHECK constraints).
"""

from decimal import Decimal
from itertools import product
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext

from apps.catalog.models import Material, UnitOfMeasure
from apps.purchasing.models import PurchaseAllocation, PurchaseOrder, PurchaseOrderLine
from apps.purchasing.transitions import ALLOWED_TRANSITIONS, is_allowed, transition
from tests import orders
from tests.org import Org, People
from tests.unit.purchasing.conftest import MakeLine

pytestmark = pytest.mark.django_db

D = Decimal
Status = PurchaseOrder.Status


DRAFT_ORDER = gettext("Draft order")


def refused(create: Any, **values: Any) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        create(**values)


def raw_order(org: Org, **values: Any) -> PurchaseOrder:
    """An order row exactly as given (``tests.orders.order`` fills the issue fields)."""
    return PurchaseOrder.objects.create(
        branch=org.a_mad,
        buyer_company=org.a,
        supplier=org.supplier,
        ship_to=org.warehouses["A Madrid"],
        **values,
    )


class TestOrder:
    def test_a_draft_has_no_number_and_shows_as_a_draft(self, org: Org) -> None:
        order = orders.order(org)
        assert (order.number, order.revision, order.currency) == (None, 0, "EUR")
        assert str(order) == DRAFT_ORDER

    def test_an_issued_order_shows_its_number(self, org: Org, people: People) -> None:
        order = orders.order(org, status=Status.ISSUED, issued_by=people.mm_mad)
        assert str(order) == order.number

    @pytest.mark.parametrize(
        "values",
        [
            {"status": "LOST"},
            {"status": Status.ISSUED},  # no number, issue time or issuer
            {"number": "PED-A-2026-00001"},  # a draft has none
            {"revision": 1},  # only issued orders have revisions
        ],
    )
    def test_constraints(self, org: Org, values: dict[str, Any]) -> None:
        refused(raw_order, org=org, **values)

    def test_a_cancelled_order_is_a_discarded_draft_or_was_issued(
        self, org: Org, people: People
    ) -> None:
        issued = {"number": "PED-X-1", "issued_at": timezone.now(), "issued_by": people.mm_mad}
        raw_order(org, status=Status.CANCELLED)
        raw_order(org, status=Status.CANCELLED, **issued)
        refused(raw_order, org=org, status=Status.CANCELLED, number="PED-X-2")


class TestLine:
    @pytest.fixture
    def draft(self, org: Org) -> PurchaseOrder:
        return orders.order(org)

    def test_a_line_carries_its_materials_unit_c9(
        self, draft: PurchaseOrder, cable: Material
    ) -> None:
        units = UnitOfMeasure.objects.exclude(pk=cable.unit_id)
        with pytest.raises(IntegrityError), transaction.atomic():
            PurchaseOrderLine.objects.create(
                po=draft, line_no=1, material=cable, unit=units[0], ordered_qty=D(1)
            )

    def test_a_referenced_materials_unit_never_changes(
        self, draft: PurchaseOrder, cable: Material
    ) -> None:
        orders.line(draft, cable, "5")
        other = UnitOfMeasure.objects.exclude(pk=cable.unit_id).first()
        with pytest.raises(IntegrityError), transaction.atomic():
            Material.objects.filter(pk=cable.pk).update(unit=other)

    @pytest.mark.parametrize(
        "values",
        [
            {"ordered": "0"},
            {"unit_price": D("-0.01")},
            {"discount_pct": D("100.01")},
            {"discount_pct": D(-1)},
            {"close_reason": "no time"},  # a reason without the closure time
            {"closed_short_at": "2026-10-31T10:00:00Z"},  # a closure without a reason
        ],
    )
    def test_checks(self, draft: PurchaseOrder, cable: Material, values: dict[str, Any]) -> None:
        refused(orders.line, po=draft, material=cable, **({"ordered": "5"} | values))

    def test_line_numbers_and_one_open_line_per_material(
        self, draft: PurchaseOrder, cable: Material, breaker: Material
    ) -> None:
        first = orders.line(draft, cable, "5")
        assert str(first) == f"{DRAFT_ORDER} #1"
        with pytest.raises(IntegrityError), transaction.atomic():
            orders.line(draft, cable, "1")  # a second open cable line
        with pytest.raises(IntegrityError), transaction.atomic():
            PurchaseOrderLine.objects.create(
                po=draft, line_no=1, material=breaker, unit=breaker.unit, ordered_qty=D(1)
            )
        PurchaseOrderLine.objects.filter(pk=first.pk).update(
            closed_short_at=first.created_at, close_reason="Short"
        )
        orders.line(draft, cable, "1")  # the closed line no longer counts

    def test_prices_and_discounts_are_optional(self, draft: PurchaseOrder, cable: Material) -> None:
        line = orders.line(draft, cable, "5", unit_price=D("12.3456"), discount_pct=D("100"))
        assert (line.unit_price, line.discount_pct) == (D("12.3456"), D(100))


class TestAllocation:
    def test_one_allocation_per_pair_never_negative(
        self, org: Org, cable: Material, make_line: MakeLine
    ) -> None:
        line = orders.line(orders.order(org), cable, "5")
        request_line = make_line("5")
        allocation = orders.allocate(line, request_line, "5")
        assert str(allocation) == f"{DRAFT_ORDER} #1 → {request_line}"
        with pytest.raises(IntegrityError), transaction.atomic():
            orders.allocate(line, request_line, "1")
        refused(orders.allocate, po_line=line, request_line=make_line("5"), qty="-1")
        assert PurchaseAllocation.objects.count() == 1


class TestTransitions:
    def test_the_table_pair_by_pair(self) -> None:
        allowed = {
            (Status.DRAFT, Status.ISSUED, "issue"),
            (Status.DRAFT, Status.CANCELLED, "discard"),
            (Status.ISSUED, Status.ISSUED, "amend"),
            (Status.ISSUED, Status.CANCELLED, "cancel"),
            (Status.ISSUED, Status.CLOSED, "auto_close"),
            (Status.CLOSED, Status.ISSUED, "auto_reopen"),
        }
        operations = {op for ops in ALLOWED_TRANSITIONS.values() for op in ops}
        for current, target, operation in product(Status, Status, sorted(operations)):
            expected = (current, target, operation) in allowed
            assert is_allowed(current, target, operation) is expected, (current, target, operation)
        assert {(a, b) for a, b, _ in allowed} == set(ALLOWED_TRANSITIONS)
        assert is_allowed(Status.DRAFT, Status.ISSUED)

    def test_transition_moves_or_refuses(self, org: Org) -> None:
        order = orders.order(org)
        assert transition(order, Status.CANCELLED, "discard") == Status.DRAFT
        assert order.status == Status.CANCELLED
        with pytest.raises(ValidationError) as exc:
            transition(order, Status.ISSUED, "issue")
        assert exc.value.code == "transition"
