"""An independent check of the quantity ledger (plan §4.5, §4.6, §4.9; M8.1).

``violations(ctx)`` reads every row of the documents that feed the ledger, derives received
R, pending P, draft reservations Q, delivered D and the allocated ceiling again from those
rows with a waterfall of its own (it never calls ``apps.purchasing.waterfall``), compares
them with what the app derives (``material_requests.ledger.flows`` and the purchasing
``Book``) and checks the invariants I1-I13, the integrity rules C4-C10 and the automatic
transitions of §4.9 (``T-ORDER``, ``T-REQUEST``). Each finding is a ``Violation`` naming
its rule.

``Watch`` adds what needs the state before an operation: a refused operation writes
nothing; posted receipts and handovers change only by their void (I11); an allocation is
created or raised only on an open line of a draft or issued order (C5); and a receipt, an
attribution, a handover line or an order line is added only to a parent that was open
(C10).

``tests/integration/test_ledger_machine.py`` runs both after every operation;
``tests/integration/test_invariant_checker.py`` breaks each rule on purpose and shows the
check notices.
"""

from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, fields
from datetime import UTC, date, datetime
from decimal import Decimal
from functools import cached_property
from typing import Any
from uuid import UUID

from django.db.models import Model, QuerySet

from apps.access.context import AccessContext
from apps.catalog.models import Material
from apps.handovers.models import Handover, HandoverLine
from apps.handovers.selectors import warehouse_stock
from apps.locations.models import Location
from apps.material_requests import ledger
from apps.material_requests.models import MaterialRequest, MaterialRequestLine
from apps.organizations.models import Branch
from apps.purchasing.ledger import book_for_nodes
from apps.purchasing.models import PurchaseAllocation, PurchaseOrder, PurchaseOrderLine
from apps.receiving.models import GoodsReceipt, GoodsReceiptLine, ReceiptAttribution

ZERO = Decimal("0")
PRIORITY_RANK = {"URGENT": 0, "HIGH": 1, "NORMAL": 2, "LOW": 3}
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
DRAFT, ISSUED, CLOSED, CANCELLED = "DRAFT", "ISSUED", "CLOSED", "CANCELLED"
POSTED, VOIDED, APPROVED = "POSTED", "VOIDED", "APPROVED"
WAREHOUSE, WORKSITE = Location.Kind.WAREHOUSE, Location.Kind.WORKSITE
DIRECT, FROM_WAREHOUSE = Handover.Kind.DIRECT_RECEIPT, Handover.Kind.FROM_WAREHOUSE
# What a void may change on a posted receipt or handover (I11).
VOID_FIELDS = frozenset({"status", "voided_at", "voided_by_id", "void_reason", "updated_at"})

type Row = dict[str, Any]
type Table = dict[UUID, Row]


@dataclass(frozen=True)
class Violation:
    rule: str
    detail: str

    def __str__(self) -> str:
        return f"{self.rule}: {self.detail}"


def _table(queryset: QuerySet[Any]) -> Table:
    return {row["id"]: row for row in queryset.values()}


@dataclass(frozen=True)
class Rows:
    """Every row the rules read, as the database holds it now."""

    requests: Table
    lines: Table
    orders: Table
    order_lines: Table
    allocations: Table
    receipts: Table
    receipt_lines: Table
    attributions: Table
    handovers: Table
    handover_lines: Table
    locations: Table
    branches: Table
    materials: Table

    @classmethod
    def load(cls, places: Rows | None = None) -> Rows:
        """Read every table; ``places``: take locations, branches and materials from these
        rows instead (no operation of the ledger changes them)."""
        documents: tuple[type[Model], ...] = (
            MaterialRequest,
            MaterialRequestLine,
            PurchaseOrder,
            PurchaseOrderLine,
            PurchaseAllocation,
            GoodsReceipt,
            GoodsReceiptLine,
            ReceiptAttribution,
            Handover,
            HandoverLine,
        )
        static: tuple[type[Model], ...] = (Location, Branch, Material)
        names = [item.name for item in fields(cls)]
        tables = {
            name: _table(model._default_manager.all())
            for name, model in zip(names, documents if places else documents + static, strict=False)
        }
        if places is not None:
            tables |= {name: getattr(places, name) for name in names[len(documents) :]}
        return cls(**tables)

    def changed_tables(self, other: Rows) -> list[str]:
        names = [item.name for item in fields(self)]
        return [name for name in names if getattr(self, name) != getattr(other, name)]


# --- the reference derivation ------------------------------------------------------------


def _total(pairs: Iterable[tuple[UUID, Decimal]]) -> dict[UUID, Decimal]:
    totals: dict[UUID, Decimal] = defaultdict(lambda: ZERO)
    for key, value in pairs:
        totals[key] += value
    return totals


def is_live(line: Row) -> bool:
    approved = line["approved_qty"]
    return (
        line["decision"] == APPROVED
        and approved is not None
        and approved > 0
        and line["closed_short_at"] is None
    )


@dataclass(frozen=True)
class Derived:
    """The ledger derived again from the rows (plan §4.5, "Ledger")."""

    received: dict[UUID, Decimal]  # order line: accepted on posted receipts
    attributed: dict[UUID, Decimal]  # allocation: attributed on posted receipts
    delivered: dict[UUID, Decimal]  # request line: handed over on posted handovers
    pending: dict[UUID, Decimal]  # allocation: its share of its line's waterfall
    reserved: dict[UUID, Decimal]  # allocation: its draft reservation

    def remaining(self, rows: Rows, order_line_id: UUID) -> Decimal:
        ordered: Decimal = rows.order_lines[order_line_id]["ordered_qty"]
        return ordered - self.received[order_line_id]

    def has_capacity(self, rows: Rows, order_line_id: UUID) -> bool:
        line = rows.order_lines[order_line_id]
        return line["closed_short_at"] is None and self.remaining(rows, order_line_id) > 0

    def receivable(self, rows: Rows, order_line_id: UUID) -> bool:
        order = rows.orders[rows.order_lines[order_line_id]["po_id"]]
        return order["status"] == ISSUED and self.has_capacity(rows, order_line_id)

    def of_request_line(self, rows: Rows, line_id: UUID) -> ledger.Flows:
        mine = [a for a in rows.allocations.values() if a["request_line_id"] == line_id]
        return ledger.Flows(
            received=sum((self.attributed[a["id"]] for a in mine), ZERO),
            pending=sum((self.pending[a["id"]] for a in mine), ZERO),
            reserved=sum((self.reserved[a["id"]] for a in mine), ZERO),
            delivered=self.delivered[line_id],
            allocated=sum(
                (a["qty"] for a in mine if _order_of(rows, a)["status"] in {DRAFT, ISSUED}),
                ZERO,
            ),
        )


def is_open(rows: Rows, line: Row) -> bool:
    return rows.requests[line["request_id"]]["status"] == APPROVED and is_live(line)


def _order_of(rows: Rows, allocation: Row) -> Row:
    return rows.orders[rows.order_lines[allocation["po_line_id"]]["po_id"]]


def _attribution_key(rows: Rows, allocation: Row) -> tuple[object, ...]:
    """Priority, needed-by (none last), submission (none last), then the allocation id."""
    request = rows.requests[rows.lines[allocation["request_line_id"]]["request_id"]]
    needed, submitted = request["needed_by"], request["submitted_at"]
    return (
        PRIORITY_RANK[request["priority"]],
        needed is None,
        needed or date.min,
        submitted is None,
        submitted or EPOCH,
        allocation["id"],
    )


def _waterfall(rows: Rows, derived: Derived, order_line_id: UUID) -> dict[UUID, Decimal]:
    """Hand the line's remaining quantity to its open allocations in attribution order."""
    mine = [a for a in rows.allocations.values() if a["po_line_id"] == order_line_id]
    if not derived.receivable(rows, order_line_id):
        return {a["id"]: ZERO for a in mine}
    left, shares = derived.remaining(rows, order_line_id), {}
    for allocation in sorted(mine, key=lambda a: _attribution_key(rows, a)):
        take = ZERO
        if is_open(rows, rows.lines[allocation["request_line_id"]]):
            ceiling = max(ZERO, allocation["qty"] - derived.attributed[allocation["id"]])
            take = min(ceiling, left)
        shares[allocation["id"]], left = take, left - take
    return shares


def _receipts(rows: Rows) -> tuple[dict[UUID, Decimal], dict[UUID, Decimal]]:
    """Received per order line and attributed per allocation, on posted receipts."""
    posted = {pk for pk, receipt in rows.receipts.items() if receipt["status"] == POSTED}
    received = _total(
        (line["po_line_id"], line["qty_received"])
        for line in rows.receipt_lines.values()
        if line["receipt_id"] in posted
    )
    attributed = _total(
        (item["allocation_id"], item["qty"])
        for item in rows.attributions.values()
        if rows.receipt_lines[item["receipt_line_id"]]["receipt_id"] in posted
    )
    return received, attributed


def _reserved(rows: Rows, allocation: Row) -> Decimal:
    draft = _order_of(rows, allocation)["status"] == DRAFT
    line_open = is_open(rows, rows.lines[allocation["request_line_id"]])
    return allocation["qty"] if draft and line_open else ZERO


def derive(rows: Rows) -> Derived:
    received, attributed = _receipts(rows)
    delivered = _total(
        (line["request_line_id"], line["qty"])
        for line in rows.handover_lines.values()
        if rows.handovers[line["handover_id"]]["status"] == POSTED
    )
    derived = Derived(received, attributed, delivered, {}, {})
    for order_line_id in rows.order_lines:
        derived.pending.update(_waterfall(rows, derived, order_line_id))
    derived.reserved.update({pk: _reserved(rows, a) for pk, a in rows.allocations.items()})
    return derived


# --- the rules ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Facts:
    rows: Rows
    derived: Derived
    ctx: AccessContext

    @cached_property
    def line_flows(self) -> dict[UUID, ledger.Flows]:
        return {pk: self.derived.of_request_line(self.rows, pk) for pk in self.rows.lines}

    def flows(self, line_id: UUID) -> ledger.Flows:
        return self.line_flows[line_id]

    def approved(self, line: Row) -> Decimal:
        return line["approved_qty"] or ZERO


def label(rows: Rows, kind: str, pk: UUID) -> str:
    """A short readable name for a row in a violation."""
    if kind == "line":
        line = rows.lines[pk]
        return f"line {line['line_no']} of {rows.requests[line['request_id']]['number']}"
    if kind == "order_line":
        line = rows.order_lines[pk]
        order = rows.orders[line["po_id"]]
        return f"line {line['line_no']} of order {order['number'] or order['id']}"
    return f"{kind} {pk}"


def _derivation(facts: Facts) -> Iterator[Violation]:
    """The app derives the same R, P, Q, D and allocated ceiling as the reference."""
    rows = facts.rows
    found = ledger.flows(list(rows.lines))
    for line_id, flows in found.items():
        expected = facts.flows(line_id)
        if flows != expected:
            name = label(rows, "line", line_id)
            yield Violation("LEDGER", f"{name}: app {flows} != reference {expected}")
    book = book_for_nodes(list(rows.order_lines), list(rows.lines))
    for pk in rows.allocations:
        listed = pk in book.allocations
        app = (book.pending(pk), book.reserved(pk)) if listed else (ZERO, ZERO)
        reference = (facts.derived.pending[pk], facts.derived.reserved[pk])
        if app != reference:
            yield Violation("LEDGER", f"allocation {pk}: app P, Q {app} != reference {reference}")


def _i1(facts: Facts) -> Iterator[Violation]:
    for pk, line in facts.rows.lines.items():
        if line["approved_qty"] is not None and line["approved_qty"] > line["requested_qty"]:
            yield Violation("I1", f"{label(facts.rows, 'line', pk)}: approved > requested")


def _i2(facts: Facts) -> Iterator[Violation]:
    for pk, line in facts.rows.lines.items():
        if not is_open(facts.rows, line):
            continue
        f = facts.flows(pk)
        committed = max(f.delivered, f.received) + f.pending + f.reserved
        if committed > facts.approved(line):
            name = label(facts.rows, "line", pk)
            yield Violation("I2", f"{name}: committed {committed} > A {line['approved_qty']}")


def _allocated(rows: Rows, order_line_id: UUID) -> Decimal:
    return sum(
        (a["qty"] for a in rows.allocations.values() if a["po_line_id"] == order_line_id), ZERO
    )


def _i3_i4(facts: Facts) -> Iterator[Violation]:
    rows = facts.rows
    for pk, line in rows.order_lines.items():
        name = label(rows, "order_line", pk)
        if _allocated(rows, pk) > line["ordered_qty"]:
            yield Violation("I3", f"{name}: allocated {_allocated(rows, pk)} > ordered")
        if facts.derived.received[pk] > line["ordered_qty"]:
            yield Violation("I4", f"{name}: received {facts.derived.received[pk]} > ordered")


def _i5_i13(facts: Facts) -> Iterator[Violation]:
    for pk, line in facts.rows.lines.items():
        f, approved = facts.flows(pk), facts.approved(line)
        if f.delivered > approved:
            yield Violation("I5", f"{label(facts.rows, 'line', pk)}: D {f.delivered} > A")
        if f.received > approved:
            yield Violation("I13", f"{label(facts.rows, 'line', pk)}: R {f.received} > A")


def _i6(facts: Facts) -> Iterator[Violation]:
    for pk, allocation in facts.rows.allocations.items():
        if facts.derived.attributed[pk] > allocation["qty"]:
            yield Violation("I6", f"allocation {pk}: attributed > qty {allocation['qty']}")


def _i7(facts: Facts) -> Iterator[Violation]:
    split = _total(
        (item["receipt_line_id"], item["qty"]) for item in facts.rows.attributions.values()
    )
    for pk, line in facts.rows.receipt_lines.items():
        if split[pk] + line["surplus_qty"] != line["qty_received"]:
            yield Violation("I7", f"receipt line {pk}: attributions + surplus != accepted")


def _direct_handover(rows: Rows, receipt_id: UUID) -> list[Row]:
    return [h for h in rows.handovers.values() if h["goods_receipt_id"] == receipt_id]


def _attributions_of(rows: Rows, receipt_id: UUID) -> list[Row]:
    return [
        a
        for a in rows.attributions.values()
        if rows.receipt_lines[a["receipt_line_id"]]["receipt_id"] == receipt_id
    ]


def _i8_receipt(rows: Rows, receipt: Row, handed: set[UUID]) -> Iterator[Violation]:
    handovers = _direct_handover(rows, receipt["id"])
    attributions = _attributions_of(rows, receipt["id"])
    site = rows.locations[receipt["location_id"]]["kind"] == WORKSITE
    expected = 1 if site and attributions else 0
    if len(handovers) != expected:
        yield Violation("I8", f"receipt {receipt['number']}: {len(handovers)} direct handovers")
    if any(h["status"] != receipt["status"] for h in handovers):
        yield Violation("I8", f"receipt {receipt['number']}: its handover has another status")
    if site and any(a["id"] not in handed for a in attributions):
        yield Violation("I8", f"receipt {receipt['number']}: an attribution not handed over")


def _i8_receipts(rows: Rows) -> Iterator[Violation]:
    """A site receipt with attributions has one direct handover, voided with it; a
    warehouse receipt has none; each site attribution is handed over once."""
    handed = {line["receipt_attribution_id"] for line in rows.handover_lines.values()}
    for receipt in rows.receipts.values():
        yield from _i8_receipt(rows, receipt, handed)


def _i8_lines(rows: Rows) -> Iterator[Violation]:
    """A direct line names one attribution of the same request line and quantity."""
    for pk, line in rows.handover_lines.items():
        kind = rows.handovers[line["handover_id"]]["kind"]
        attribution = rows.attributions.get(line["receipt_attribution_id"])
        if kind == FROM_WAREHOUSE:
            if attribution is not None:
                yield Violation("I8", f"warehouse handover line {pk} names an attribution")
            continue
        if attribution is None or (
            attribution["qty"],
            rows.allocations[attribution["allocation_id"]]["request_line_id"],
        ) != (line["qty"], line["request_line_id"]):
            yield Violation("I8", f"direct handover line {pk} does not match its attribution")


def _i8(facts: Facts) -> Iterator[Violation]:
    yield from _i8_receipts(facts.rows)
    yield from _i8_lines(facts.rows)


def _stock(rows: Rows, warehouse: UUID, material: UUID) -> Decimal:
    received = sum(
        (
            line["qty_received"]
            for line in rows.receipt_lines.values()
            if rows.receipts[line["receipt_id"]]["status"] == POSTED
            and rows.receipts[line["receipt_id"]]["location_id"] == warehouse
            and rows.order_lines[line["po_line_id"]]["material_id"] == material
        ),
        ZERO,
    )
    handed = sum(
        (
            line["qty"]
            for line in rows.handover_lines.values()
            if rows.handovers[line["handover_id"]]["status"] == POSTED
            and rows.handovers[line["handover_id"]]["kind"] == FROM_WAREHOUSE
            and rows.handovers[line["handover_id"]]["source_location_id"] == warehouse
            and rows.lines[line["request_line_id"]]["material_id"] == material
        ),
        ZERO,
    )
    return max(ZERO, received - handed)


def _i9(facts: Facts) -> Iterator[Violation]:
    """Availability counts posted receipts at that warehouse only (a site receipt never).

    Asked of the warehouses where a receipt arrived or a handover left from (or where the
    reference sees one): elsewhere both sides are zero."""
    rows, materials = facts.rows, list(facts.rows.materials)
    used = {receipt["location_id"] for receipt in rows.receipts.values()} | {
        handover["source_location_id"] for handover in rows.handovers.values()
    }
    for pk, location in rows.locations.items():
        if location["kind"] != WAREHOUSE or pk not in used:
            continue
        shown = warehouse_stock(facts.ctx, pk, materials)
        expected = {material: _stock(rows, pk, material) for material in materials}
        if shown != expected:
            yield Violation("I9", f"warehouse {location['name']}: {shown} != {expected}")


def _negative(rows: Rows) -> Iterator[tuple[str, Decimal, bool]]:
    """(what, quantity, may be zero) for every stored quantity."""
    for line in rows.lines.values():
        yield "requested", line["requested_qty"], False
        yield "approved", line["approved_qty"] or ZERO, True
    for line in rows.order_lines.values():
        yield "ordered", line["ordered_qty"], False
    for allocation in rows.allocations.values():
        yield "allocation", allocation["qty"], True
    for line in rows.receipt_lines.values():
        yield "accepted", line["qty_received"], True
        yield "surplus", line["surplus_qty"], True
    for item in rows.attributions.values():
        yield "attribution", item["qty"], False
    for line in rows.handover_lines.values():
        yield "handed over", line["qty"], False


def _i10(facts: Facts) -> Iterator[Violation]:
    for what, quantity, zero in _negative(facts.rows):
        if quantity < 0 or (quantity == 0 and not zero):
            yield Violation("I10", f"{what} quantity {quantity}")


def _i12(facts: Facts) -> Iterator[Violation]:
    rows, derived = facts.rows, facts.derived
    for pk, allocation in rows.allocations.items():
        line_open = is_open(rows, rows.lines[allocation["request_line_id"]])
        receivable = derived.receivable(rows, allocation["po_line_id"])
        if derived.pending[pk] > 0 and not (line_open and receivable):
            yield Violation("I12", f"allocation {pk}: pending off a receivable open line")
        if derived.reserved[pk] > 0 and _order_of(rows, allocation)["status"] != DRAFT:
            yield Violation("I12", f"allocation {pk}: reserved outside a draft")
    pending = _total((a["po_line_id"], derived.pending[pk]) for pk, a in rows.allocations.items())
    for pk in rows.order_lines:
        if pending[pk] > max(ZERO, derived.remaining(rows, pk)):
            yield Violation("I12", f"{label(rows, 'order_line', pk)}: pending > remaining")


def _c4(facts: Facts) -> Iterator[Violation]:
    """A direct line's attribution is of its handover's own receipt."""
    rows = facts.rows
    for pk, line in rows.handover_lines.items():
        handover = rows.handovers[line["handover_id"]]
        attribution = rows.attributions.get(line["receipt_attribution_id"])
        if handover["kind"] != DIRECT or attribution is None:
            continue
        receipt_id = rows.receipt_lines[attribution["receipt_line_id"]]["receipt_id"]
        if receipt_id != handover["goods_receipt_id"]:
            yield Violation("C4", f"direct handover line {pk}: attribution of another receipt")


def connects(rows: Rows, allocation: Row) -> bool:
    """A positive ceiling on an order that is not cancelled (the app's ``LIVE``)."""
    return bool(allocation["qty"] > 0 and _order_of(rows, allocation)["status"] != CANCELLED)


def _c5(facts: Facts) -> Iterator[Violation]:
    """Same material, unit and procurement branch on both sides of an allocation that
    connects them.

    An allocation trimmed to zero connects nothing and can never take pending again (its
    ceiling is spent, and raising it is a C5 increase): a reviewer may change its request
    line's material in place (M7b.3), so it is not compared. That the line is to purchase
    is part of the increase (``history``): a receipt void may revive pending on a line
    switched to stock after its goods arrived, which no rule of the plan forbids."""
    rows = facts.rows
    for pk, allocation in rows.allocations.items():
        line = rows.lines[allocation["request_line_id"]]
        order_line = rows.order_lines[allocation["po_line_id"]]
        branch = rows.requests[line["request_id"]]["procurement_branch_id"]
        same = (line["material_id"], line["unit_id"], branch) == (
            order_line["material_id"],
            order_line["unit_id"],
            _order_of(rows, allocation)["branch_id"],
        )
        if connects(rows, allocation) and not same:
            yield Violation("C5", f"allocation {pk}: material, unit or branch differ")


def _c6(facts: Facts) -> Iterator[Violation]:
    rows = facts.rows
    for pk, allocation in rows.allocations.items():
        ship_to = rows.locations[_order_of(rows, allocation)["ship_to_id"]]
        project = rows.lines[allocation["request_line_id"]]["project_id"]
        if ship_to["kind"] == WORKSITE and ship_to["project_id"] != project:
            yield Violation("C6", f"allocation {pk}: another project than the order's worksite")


def _scope(rows: Rows, location: Row) -> set[UUID]:
    """The branches whose documents may use this warehouse: its own and its manager."""
    branch = rows.branches[location["branch_id"]]
    return {branch["id"], branch["managed_by_id"]} - {None}


def _c7_receipts(rows: Rows) -> Iterator[Violation]:
    for receipt in rows.receipts.values():
        location, order = rows.locations[receipt["location_id"]], rows.orders[receipt["po_id"]]
        if location["kind"] == WAREHOUSE:
            valid = order["branch_id"] in _scope(rows, location)
        else:
            valid = any(
                _order_of(rows, a)["id"] == order["id"]
                and rows.lines[a["request_line_id"]]["project_id"] == location["project_id"]
                for a in rows.allocations.values()
            )
        if not valid:
            yield Violation("C7", f"receipt {receipt['number']}: goods arrived where they may not")


def _c7_handovers(rows: Rows) -> Iterator[Violation]:
    for handover in rows.handovers.values():
        source = rows.locations[handover["source_location_id"]]
        if handover["kind"] == FROM_WAREHOUSE:
            valid = source["kind"] == WAREHOUSE and handover["branch_id"] in _scope(rows, source)
        else:
            receipt = rows.receipts[handover["goods_receipt_id"]]
            valid = source["id"] == receipt["location_id"]
        if not valid:
            yield Violation("C7", f"handover {handover['number']}: wrong source")


def _c7(facts: Facts) -> Iterator[Violation]:
    yield from _c7_receipts(facts.rows)
    yield from _c7_handovers(facts.rows)


def _c8(facts: Facts) -> Iterator[Violation]:
    rows = facts.rows
    for receipt in rows.receipts.values():
        if receipt["branch_id"] != rows.orders[receipt["po_id"]]["branch_id"]:
            yield Violation("C8", f"receipt {receipt['number']}: not its order's branch")
    for pk, line in rows.handover_lines.items():
        handover = rows.handovers[line["handover_id"]]
        request_line = rows.lines[line["request_line_id"]]
        request = rows.requests[request_line["request_id"]]
        if (handover["branch_id"], handover["project_id"]) != (
            request["procurement_branch_id"],
            request_line["project_id"],
        ):
            yield Violation("C8", f"handover line {pk}: another branch or project")


def _c9(facts: Facts) -> Iterator[Violation]:
    rows = facts.rows
    for table in (rows.lines, rows.order_lines):
        for pk, line in table.items():
            material = rows.materials.get(line["material_id"])
            if material is not None and material["unit_id"] != line["unit_id"]:
                yield Violation("C9", f"line {pk}: not its material's unit")


def _c10(facts: Facts) -> Iterator[Violation]:
    """Nothing stays attached to a cancelled order (the history part is in ``Watch``)."""
    rows = facts.rows
    for pk, allocation in rows.allocations.items():
        if _order_of(rows, allocation)["status"] == CANCELLED and allocation["qty"] > 0:
            yield Violation("C10", f"allocation {pk}: left on a cancelled order")
    for receipt in rows.receipts.values():  # a voided one never counted: the order may go
        posted = receipt["status"] == POSTED
        if posted and rows.orders[receipt["po_id"]]["status"] in {DRAFT, CANCELLED}:
            yield Violation("C10", f"receipt {receipt['number']}: on a draft or cancelled order")


def _t_order(facts: Facts) -> Iterator[Violation]:
    """ISSUED while a line has capacity, CLOSED once none has (§4.9)."""
    rows, derived = facts.rows, facts.derived
    for pk, order in rows.orders.items():
        lines = [line_id for line_id, line in rows.order_lines.items() if line["po_id"] == pk]
        capacity = any(derived.has_capacity(rows, line_id) for line_id in lines)
        if (order["status"] == ISSUED and not capacity) or (order["status"] == CLOSED and capacity):
            yield Violation("T-ORDER", f"order {order['number']} is {order['status']}")


def waiting(rows: Rows, derived: Derived, request_id: UUID) -> bool:
    """A live line of the request still has D < A."""
    return any(
        is_live(line) and derived.delivered[line_id] < (line["approved_qty"] or ZERO)
        for line_id, line in rows.lines.items()
        if line["request_id"] == request_id
    )


def _t_request(facts: Facts) -> Iterator[Violation]:
    """A request closed automatically has nothing left to deliver (§4.9).

    The other half needs the state before: an approved request may hold no live line
    between a manual reopen and the reopening of its lines one by one (§4.9), so only an
    operation that delivers or closes its last open quantity must close it (``history``).
    """
    rows = facts.rows
    for pk, request in rows.requests.items():
        auto_closed = request["status"] == CLOSED and request["close_mode"] == "AUTO"
        if auto_closed and waiting(rows, facts.derived, pk):
            yield Violation("T-REQUEST", f"request {request['number']} is {request['status']}")


RULES: dict[str, Callable[[Facts], Iterable[Violation]]] = {
    "LEDGER": _derivation,
    "I1": _i1,
    "I2": _i2,
    "I3-I4": _i3_i4,
    "I5-I13": _i5_i13,
    "I6": _i6,
    "I7": _i7,
    "I8": _i8,
    "I9": _i9,
    "I10": _i10,
    "I12": _i12,
    "C4": _c4,
    "C5": _c5,
    "C6": _c6,
    "C7": _c7,
    "C8": _c8,
    "C9": _c9,
    "C10": _c10,
    "T-ORDER": _t_order,
    "T-REQUEST": _t_request,
}


def violations(ctx: AccessContext, rows: Rows | None = None) -> list[Violation]:
    """Every rule broken by the current state (``ctx`` reads warehouse stock, I9)."""
    rows = rows or Rows.load()
    facts = Facts(rows, derive(rows), ctx)
    return [found for rule in RULES.values() for found in rule(facts)]


# --- the rules that need the state before ------------------------------------------------


def _without(row: Row, names: frozenset[str]) -> Row:
    return {name: value for name, value in row.items() if name not in names}


def _may_change(old: Row, new: Row, *, header: bool) -> frozenset[str]:
    """A void may change a posted header's void fields; nothing else changes."""
    if not header:
        return frozenset()
    voiding = old["status"] == POSTED and new["status"] == VOIDED
    return VOID_FIELDS if voiding else frozenset({"updated_at"})


def _frozen(kind: str, before: Table, now: Table, *, header: bool) -> Iterator[Violation]:
    """I11: a posted document changes only by its void; its lines never change."""
    for pk, old in before.items():
        new = now.get(pk)
        if new is None:
            yield Violation("I11", f"{kind} {pk} was deleted")
        elif _without(old, free := _may_change(old, new, header=header)) != _without(new, free):
            yield Violation("I11", f"{kind} {pk} changed")


def _may_raise(before: Rows, before_derived: Derived, now: Rows, allocation: Row) -> bool:
    """C5 as it stood before the operation: a draft or issued order, an order line not
    closed short (with capacity once issued), an open line to purchase."""
    order_id = now.order_lines[allocation["po_line_id"]]["po_id"]
    order = before.orders.get(order_id, now.orders[order_id])
    line = before.lines[allocation["request_line_id"]]
    order_line = before.order_lines.get(allocation["po_line_id"])
    issued = order_line is not None and order["status"] == ISSUED
    if issued and not before_derived.has_capacity(before, allocation["po_line_id"]):
        return False
    return (
        order["status"] in {DRAFT, ISSUED}
        and is_open(before, line)
        and line["sourcing"] == "PURCHASE"
        and (order_line is None or order_line["closed_short_at"] is None)
    )


def _raised(before: Rows, before_derived: Derived, now: Rows) -> Iterator[Violation]:
    """C5: an allocation is created or raised only where the rule allowed it before."""
    for pk, allocation in now.allocations.items():
        old = before.allocations.get(pk)
        if old is not None and allocation["qty"] <= old["qty"]:
            continue
        if not _may_raise(before, before_derived, now, allocation):
            yield Violation("C5", f"allocation {pk} raised against C5")


def _new(before: Table, now: Table) -> Iterator[tuple[UUID, Row]]:
    return ((pk, row) for pk, row in now.items() if pk not in before)


def _attached_to_orders(before: Rows, now: Rows) -> Iterator[Violation]:
    for _pk, receipt in _new(before.receipts, now.receipts):
        if before.orders[receipt["po_id"]]["status"] != ISSUED:
            yield Violation("C10", f"receipt {receipt['number']} on an order not issued")
    for pk, item in _new(before.order_lines, now.order_lines):
        order = before.orders.get(item["po_id"], now.orders[item["po_id"]])
        if order["status"] not in {DRAFT, ISSUED}:
            yield Violation("C10", f"order line {pk} added to a {order['status']} order")


def _attached_to_lines(before: Rows, now: Rows) -> Iterator[Violation]:
    for pk, item in _new(before.attributions, now.attributions):
        line = before.lines[now.allocations[item["allocation_id"]]["request_line_id"]]
        if not is_open(before, line):
            yield Violation("C10", f"attribution {pk} to a line that was not open")
    for pk, item in _new(before.handover_lines, now.handover_lines):
        if not is_open(before, before.lines[item["request_line_id"]]):
            yield Violation("C10", f"handover line {pk} for a line that was not open")


def _attached(before: Rows, now: Rows) -> Iterator[Violation]:
    """C10: new receipts, attributions, handover lines and order lines only on open parents."""
    yield from _attached_to_orders(before, now)
    yield from _attached_to_lines(before, now)


def _not_closed(before: Rows, now: Rows) -> Iterator[Violation]:
    """§4.9: an operation that leaves an approved request with nothing to deliver, where it
    had something before, closes it."""
    before_derived, now_derived = derive(before), derive(now)
    for pk, request in now.requests.items():
        if (
            request["status"] == APPROVED
            and pk in before.requests
            and waiting(before, before_derived, pk)
            and not waiting(now, now_derived, pk)
        ):
            yield Violation("T-REQUEST", f"request {request['number']} was not closed")


def history(before: Rows, now: Rows) -> list[Violation]:
    """The rules that compare the state after a successful operation with the one before."""
    return [
        *_not_closed(before, now),
        *_frozen("receipt", _posted(before.receipts), now.receipts, header=True),
        *_frozen("receipt line", before.receipt_lines, now.receipt_lines, header=False),
        *_frozen("attribution", before.attributions, now.attributions, header=False),
        *_frozen("handover", _posted(before.handovers), now.handovers, header=True),
        *_frozen("handover line", before.handover_lines, now.handover_lines, header=False),
        *_frozen("voided receipt", _voided(before.receipts), now.receipts, header=False),
        *_frozen("voided handover", _voided(before.handovers), now.handovers, header=False),
        *_raised(before, derive(before), now),
        *_attached(before, now),
    ]


def _posted(table: Table) -> Table:
    return {pk: row for pk, row in table.items() if row["status"] == POSTED}


def _voided(table: Table) -> Table:
    return {pk: row for pk, row in table.items() if row["status"] == VOIDED}


class Watch:
    """Checks every rule after each operation, against the state the operation found."""

    def __init__(self, ctx: AccessContext) -> None:
        self.ctx = ctx
        self.rows = Rows.load()

    def step(self, *, refused: bool) -> list[Violation]:
        before = self.rows
        now = Rows.load(places=before)
        self.rows = now
        if refused:
            changed = before.changed_tables(now)
            return [Violation("REFUSED", f"a refused operation wrote {changed}")] if changed else []
        return [*history(before, now), *violations(self.ctx, now)]
