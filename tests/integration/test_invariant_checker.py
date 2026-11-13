"""The invariant checker notices every broken rule (M8.1 done-when).

The documents are built once through the services: an issued order for X, Y and Z
received at A-MAD's warehouse and at MAD-2's site (a direct handover), a voided receipt, a
warehouse handover to X and a draft for W. They break no rule. Each case then breaks one
rule in a copy of the rows (the database constraints and triggers would refuse most of
these writes, which is why the copy is changed, not the database) and expects the checker
to name it. The state machine relies on these checks; a check that could not fail would
let a broken service through.
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from django.db import transaction
from django.utils import timezone

from apps.access.principals import SessionPrincipal
from apps.catalog.models import UnitOfMeasure
from apps.handovers import services as handovers
from apps.handovers.models import Handover
from apps.handovers.services import HandoverLineInput, Receiver
from apps.material_requests import ledger
from apps.purchasing import services as purchasing
from apps.purchasing.models import PurchaseAllocation, PurchaseOrder
from apps.purchasing.services import RequestItem
from apps.receiving import services as receiving
from apps.receiving.models import GoodsReceipt, ReceiptAttribution
from apps.receiving.services import ReceiptLineInput
from tests.invariants import RULES, Derived, Facts, Rows, Watch, derive, history, violations
from tests.ledger_world import LedgerWorld, build_ledger_world
from tests.org import build_org, build_people
from tests.support import principal_for

pytestmark = [pytest.mark.integration, pytest.mark.django_db]

D = Decimal


@dataclass(frozen=True)
class Documents:
    world: LedgerWorld
    issued: PurchaseOrder  # X 10, Y 8, Z 6; 24 ordered
    draft: PurchaseOrder  # W 7
    at_warehouse: GoodsReceipt  # 10 at A-MAD: Y 8 (HIGH), X 2
    at_site: GoodsReceipt  # 4 at MAD-2's site by A-MAD's technician (Q44): Z 4, handed over
    voided: GoodsReceipt  # 1 at A-MAD, voided
    handover: Handover  # X 2 from A-MAD's warehouse

    def line(self, name: str) -> UUID:
        return self.world.lines[name].pk

    def allocation(self, name: str) -> UUID:
        return PurchaseAllocation.objects.get(request_line_id=self.line(name)).pk

    def attribution(self, receipt: GoodsReceipt, name: str) -> UUID:
        return ReceiptAttribution.objects.get(
            receipt_line__receipt=receipt, allocation__request_line_id=self.line(name)
        ).pk

    @property
    def direct(self) -> Handover:
        return Handover.objects.get(goods_receipt=self.at_site)


def _build(world: LedgerWorld) -> Documents:
    buyer, lines, madrid = world.buyer, world.lines, world.warehouses["A Madrid"].pk

    def order(*items: tuple[str, str]) -> PurchaseOrder:
        po = purchasing.create_po(
            buyer,
            branch_id=world.org.a_mad.pk,
            supplier_id=world.org.supplier.pk,
            ship_to_id=madrid,
        )
        requested = [RequestItem(lines[name].pk, D(qty)) for name, qty in items]
        return purchasing.add_request_lines(buyer, po.pk, requested)

    def receive(at: UUID, qty: str, by: SessionPrincipal = buyer) -> GoodsReceipt:
        line = issued.lines.get().pk
        return receiving.post_receipt(
            by, issued.pk, location_id=at, lines=[ReceiptLineInput(line, D(qty))]
        )

    issued = purchasing.issue(buyer, order(("X", "10"), ("Y", "8"), ("Z", "6")).pk)
    voided = receive(madrid, "1")
    receiving.void_receipt(buyer, voided.pk, reason="Wrong order")
    return Documents(
        world=world,
        issued=issued,
        draft=order(("W", "7")),
        at_warehouse=receive(madrid, "10"),
        at_site=receive(
            world.worksite(world.projects[1].pk).pk, "4", principal_for(world.people.tech_mad)
        ),
        voided=GoodsReceipt.objects.get(pk=voided.pk),
        handover=handovers.create_handover(
            buyer,
            world.org.hospital.pk,
            source_location_id=madrid,
            lines=[HandoverLineInput(lines["X"].pk, D(2))],
            receiver=Receiver(name="Site crew"),
        ),
    )


@pytest.fixture(scope="module")
def documents(django_db_setup: None, django_db_blocker: Any) -> Iterator[Documents]:
    with django_db_blocker.unblock(), transaction.atomic():
        org = build_org()
        yield _build(build_ledger_world(org, build_people(org)))
        transaction.set_rollback(True)


def test_the_documents_break_no_rule(documents: Documents) -> None:
    assert violations(documents.world.ctx) == []
    flows = ledger.flows([documents.line(name) for name in "XYZ"])
    assert [(f.received, f.delivered) for f in flows.values()] == [
        (D(2), D(2)),  # X: 2 at the warehouse, 2 handed over from it
        (D(8), D(0)),  # Y: first in the attribution order (HIGH)
        (D(4), D(4)),  # Z: received and handed over on site
    ]


# --- one broken rule per case, in a copy of the rows ------------------------------------

type Break = Callable[[Rows, Documents], None]


def _set(table: dict[UUID, dict[str, Any]], pk: UUID, **values: Any) -> None:
    table[pk].update(values)


def _line_of(rows: Rows, receipt: GoodsReceipt) -> UUID:
    return next(pk for pk, line in rows.receipt_lines.items() if line["receipt_id"] == receipt.pk)


def _derivation(rows: Rows, docs: Documents) -> None:
    _set(rows.allocations, docs.allocation("W"), qty=D(6))  # Q 6 where the app reads 7


def _i1(rows: Rows, docs: Documents) -> None:
    _set(rows.lines, docs.line("X"), approved_qty=D(11))


def _i2(rows: Rows, docs: Documents) -> None:
    _set(rows.lines, docs.line("Y"), approved_qty=D(1))  # R 8 already


def _i3(rows: Rows, docs: Documents) -> None:
    order_line = rows.allocations[docs.allocation("W")]["po_line_id"]
    _set(rows.order_lines, order_line, ordered_qty=D(6))


def _i4(rows: Rows, docs: Documents) -> None:
    _set(rows.receipt_lines, _line_of(rows, docs.at_warehouse), qty_received=D(30))


def _handover_line(rows: Rows, handover: Handover) -> UUID:
    return next(pk for pk, row in rows.handover_lines.items() if row["handover_id"] == handover.pk)


def _i5(rows: Rows, docs: Documents) -> None:
    _set(rows.handover_lines, _handover_line(rows, docs.handover), qty=D(11))


def _i6(rows: Rows, docs: Documents) -> None:
    _set(rows.allocations, docs.allocation("Y"), qty=D(7))  # Y has 8 attributed


def _i7(rows: Rows, docs: Documents) -> None:
    _set(rows.receipt_lines, _line_of(rows, docs.at_warehouse), surplus_qty=D(1))


def _i8_status(rows: Rows, docs: Documents) -> None:
    _set(rows.handovers, docs.direct.pk, status="VOIDED")


def _i8_quantity(rows: Rows, docs: Documents) -> None:
    _set(rows.handover_lines, _handover_line(rows, docs.direct), qty=D(3))


def _i8_not_handed_over(rows: Rows, docs: Documents) -> None:
    _set(rows.handover_lines, _handover_line(rows, docs.direct), receipt_attribution_id=None)


def _i8_receiver(rows: Rows, docs: Documents) -> None:
    buyer = docs.world.people.mm_mad.pk  # the technician posted it
    _set(rows.handovers, docs.direct.pk, handed_by_id=buyer, received_by_id=buyer)


def _i9(rows: Rows, docs: Documents) -> None:
    madrid = docs.world.warehouses["A Madrid"].pk
    _set(rows.receipts, docs.at_site.pk, location_id=madrid)  # a site receipt counted as stock


def _i10(rows: Rows, docs: Documents) -> None:
    _set(rows.allocations, docs.allocation("X"), qty=D(-1))


def _i13(rows: Rows, docs: Documents) -> None:
    _set(rows.lines, docs.line("Y"), approved_qty=D(7), requested_qty=D(7))


def _c4(rows: Rows, docs: Documents) -> None:
    _set(rows.handovers, docs.direct.pk, goods_receipt_id=docs.voided.pk)


def _c5_material(rows: Rows, docs: Documents) -> None:
    order_line = rows.allocations[docs.allocation("X")]["po_line_id"]
    _set(rows.order_lines, order_line, material_id=docs.world.materials[1].pk)


def _c6(rows: Rows, docs: Documents) -> None:
    hospital = docs.world.worksite(docs.world.org.hospital.pk).pk
    _set(rows.orders, docs.issued.pk, ship_to_id=hospital)  # Z is of MAD-2


def _c7_receipt(rows: Rows, docs: Documents) -> None:
    sevilla = docs.world.warehouses["A Sevilla"].pk
    _set(rows.receipts, docs.at_warehouse.pk, location_id=sevilla)


def _c7_handover(rows: Rows, docs: Documents) -> None:
    sevilla = docs.world.warehouses["A Sevilla"].pk
    _set(rows.handovers, docs.handover.pk, source_location_id=sevilla)


def _c8_receipt(rows: Rows, docs: Documents) -> None:
    _set(rows.receipts, docs.at_warehouse.pk, branch_id=docs.world.org.a_sev.pk)


def _c8_handover(rows: Rows, docs: Documents) -> None:
    _set(rows.handovers, docs.handover.pk, branch_id=docs.world.org.a_sev.pk)


def _c9(rows: Rows, docs: Documents) -> None:
    _set(rows.lines, docs.line("X"), unit_id=UnitOfMeasure.objects.get(code="UD").pk)


def _c10(rows: Rows, docs: Documents) -> None:
    _set(rows.orders, docs.draft.pk, status="CANCELLED")  # W's 7 still allocated


def _c10_receipt(rows: Rows, docs: Documents) -> None:
    _set(rows.orders, docs.issued.pk, status="CANCELLED")  # with goods received on it


def _t_order(rows: Rows, docs: Documents) -> None:
    _set(rows.orders, docs.issued.pk, status="CLOSED")  # 10 left to receive


def _t_request(rows: Rows, docs: Documents) -> None:
    request = rows.lines[docs.line("X")]["request_id"]
    _set(rows.requests, request, status="CLOSED", close_mode="AUTO", closed_at=timezone.now())


BREAKS: dict[str, tuple[str, Break]] = {
    "derivation": ("LEDGER", _derivation),
    "approved-above-requested": ("I1", _i1),
    "over-committed": ("I2", _i2),
    "over-allocated": ("I3", _i3),
    "over-received": ("I4", _i4),
    "over-delivered": ("I5", _i5),
    "attributed-above-ceiling": ("I6", _i6),
    "split-not-summing": ("I7", _i7),
    "direct-handover-status": ("I8", _i8_status),
    "direct-line-quantity": ("I8", _i8_quantity),
    "site-attribution-not-handed-over": ("I8", _i8_not_handed_over),
    "direct-handover-by-another-receiver": ("I8", _i8_receiver),
    "site-receipt-as-stock": ("I9", _i9),
    "negative-quantity": ("I10", _i10),
    "received-above-approved": ("I13", _i13),
    "direct-line-of-another-receipt": ("C4", _c4),
    "material-differs": ("C5", _c5_material),
    "worksite-of-another-project": ("C6", _c6),
    "receipt-outside-scope": ("C7", _c7_receipt),
    "handover-source-outside-scope": ("C7", _c7_handover),
    "receipt-branch": ("C8", _c8_receipt),
    "handover-branch": ("C8", _c8_handover),
    "unit-not-the-materials": ("C9", _c9),
    "allocation-on-cancelled-order": ("C10", _c10),
    "posted-receipt-on-cancelled-order": ("C10", _c10_receipt),
    "issued-order-closed": ("T-ORDER", _t_order),
    "request-closed-with-something-to-deliver": ("T-REQUEST", _t_request),
}


@pytest.mark.parametrize(("rule", "corrupt"), BREAKS.values(), ids=BREAKS.keys())
def test_a_broken_rule_is_found(documents: Documents, rule: str, corrupt: Break) -> None:
    rows = Rows.load()
    corrupt(rows, documents)
    assert rule in {found.rule for found in violations(documents.world.ctx, rows)}


def test_pending_and_reservations_only_where_they_may_be(documents: Documents) -> None:
    """I12, on the derived figures (the derivation check holds the app to the same)."""
    rows = Rows.load()
    derived = derive(rows)
    wrong = Derived(
        derived.received,
        derived.attributed,
        derived.delivered,
        derived.pending | {documents.allocation("W"): D(1)},  # pending on a draft
        derived.reserved | {documents.allocation("X"): D(1)},  # reserved on an issued order
    )
    found = list(RULES["I12"](Facts(rows, wrong, documents.world.ctx)))
    assert len(found) == 2 and {item.rule for item in found} == {"I12"}


# --- the rules that need the state before ------------------------------------------------

type Before = Callable[[Rows, Rows, Documents], None]


def _changed_receipt(before: Rows, now: Rows, docs: Documents) -> None:
    _set(now.receipts, docs.at_warehouse.pk, delivery_note_ref="ALB-2")


def _changed_receipt_line(before: Rows, now: Rows, docs: Documents) -> None:
    _set(now.receipt_lines, _line_of(now, docs.at_site), qty_rejected=D(1))


def _unvoided_receipt(before: Rows, now: Rows, docs: Documents) -> None:
    _set(now.receipts, docs.voided.pk, status="POSTED", voided_at=None)


def _deleted_handover_line(before: Rows, now: Rows, docs: Documents) -> None:
    del now.handover_lines[_handover_line(now, docs.handover)]


def _raised_on_a_closed_line(before: Rows, now: Rows, docs: Documents) -> None:
    _set(before.allocations, docs.allocation("X"), qty=D(9))
    _set(before.lines, docs.line("X"), closed_short_at=timezone.now())


def _raised_for_stock(before: Rows, now: Rows, docs: Documents) -> None:
    _set(before.allocations, docs.allocation("W"), qty=D(5))
    _set(before.lines, docs.line("W"), sourcing="STOCK")


def _allocated_on_a_closed_order(before: Rows, now: Rows, docs: Documents) -> None:
    del before.allocations[docs.allocation("W")]
    _set(before.orders, docs.draft.pk, status="CANCELLED")


def _receipt_on_an_order_not_issued(before: Rows, now: Rows, docs: Documents) -> None:
    del before.receipts[docs.at_warehouse.pk]
    _set(before.orders, docs.issued.pk, status="CLOSED")


def _handed_over_to_a_closed_line(before: Rows, now: Rows, docs: Documents) -> None:
    del before.handover_lines[_handover_line(now, docs.handover)]
    _set(before.lines, docs.line("X"), closed_short_at=timezone.now())


def _attributed_to_a_closed_request(before: Rows, now: Rows, docs: Documents) -> None:
    del before.attributions[docs.attribution(docs.at_site, "Z")]
    request = before.lines[docs.line("Z")]["request_id"]
    _set(before.requests, request, status="CLOSED")


def _left_open_with_nothing_to_deliver(before: Rows, now: Rows, docs: Documents) -> None:
    for name in ("X", "S"):  # SOL-L-00001's two lines, both still to deliver before
        _set(now.lines, docs.line(name), closed_short_at=timezone.now())


HISTORY: dict[str, tuple[str, Before]] = {
    "posted-receipt-edited": ("I11", _changed_receipt),
    "receipt-line-edited": ("I11", _changed_receipt_line),
    "void-undone": ("I11", _unvoided_receipt),
    "handover-line-deleted": ("I11", _deleted_handover_line),
    "raised-on-a-closed-line": ("C5", _raised_on_a_closed_line),
    "raised-for-a-line-from-stock": ("C5", _raised_for_stock),
    "allocated-on-a-cancelled-order": ("C5", _allocated_on_a_closed_order),
    "receipt-on-a-closed-order": ("C10", _receipt_on_an_order_not_issued),
    "handed-over-to-a-closed-line": ("C10", _handed_over_to_a_closed_line),
    "attributed-to-a-closed-request": ("C10", _attributed_to_a_closed_request),
    "request-not-closed-automatically": ("T-REQUEST", _left_open_with_nothing_to_deliver),
}


@pytest.mark.parametrize(("rule", "corrupt"), HISTORY.values(), ids=HISTORY.keys())
def test_a_broken_rule_of_history_is_found(
    documents: Documents, rule: str, corrupt: Before
) -> None:
    before, now = Rows.load(), Rows.load()
    assert history(before, now) == []
    corrupt(before, now, documents)
    assert rule in {found.rule for found in history(before, now)}


def test_a_refusal_that_wrote_something_is_found(documents: Documents) -> None:
    watch = Watch(documents.world.ctx)
    assert watch.step(refused=True) == []
    _set(watch.rows.allocations, documents.allocation("X"), qty=D(0))  # what it found
    assert [str(found) for found in watch.step(refused=True)] == [
        "REFUSED: a refused operation wrote ['allocations']"
    ]
    assert watch.step(refused=False) == []
