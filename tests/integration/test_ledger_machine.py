"""A state machine over the whole ledger (plan §4.5-4.9, M8.1).

Random sequences of the operations that feed the ledger, each through its service as
A-MAD's material manager on the world of ``tests/ledger_world.py``: draft allocations and
increases, unallocations and ordered quantities, issues, amendments (quantities,
allocations, new lines), cancellations, order lines closed short, receipts at warehouses
and worksites (partial, surplus, manual splits; also by a technician or a partner, Q44)
and their voids, warehouse handovers and their voids, request lines and requests closed
and reopened, priority and needed-by changes, approved quantities reduced, sourcing
changes and reviewer edits (quantity and material, so lines are replaced). Buying a line
again after its order gave up is the replacement purchase.

After every step ``tests.invariants.Watch`` checks I1-I13, C4-C10, the automatic
transitions of §4.9 and the derivation itself against an independent reference; a refused
step (``ValidationError``) must have written nothing. Some steps also know their outcome:
a receipt at A-MAD's warehouse is accepted exactly when the line can take it (I4), a
warehouse handover exactly when it fits the line's outstanding quantity (I5; the stock is a
warning, never a limit, I9), a posted receipt or warehouse handover can always be voided
and a direct handover never on its own, a request reopen keeps the lines closed one by one,
and an allocation raised beyond its line's *to purchase* is refused (C5; the reconciliation
after it would otherwise trim the excess away unseen).

Each example starts from one of a few productive states (``@initialize``) and runs in a
savepoint rolled back at its end. Rules pick targets the operation can apply to when there
are some (an open line to hand over, an issued order to receive); the quantities still
reach past what is allowed, so refusals are exercised too. The operations that give
quantities up share one rule, so an example rarely ends with nothing left to do. In CI
Hypothesis loads its ``ci`` profile: the sequences are derandomized and a failure prints
its reproduction blob. ``LEDGER_MACHINE_EXAMPLES`` and ``LEDGER_MACHINE_STEPS`` raise the
effort of a local, random run.

``test_a_tour`` drives the same checked operations through fixed sequences: every operation
once, and the shapes a cap or a reopening exists for (E8, E12 with stock handed over first,
E13, a warehouse handover closing its request, a partner and a technician receiving at their
projects), which random walks seldom build. Breaking any of those rules in the services
fails a tour.
"""

import os
from collections.abc import Callable
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, ClassVar
from uuid import UUID

import pytest
from django.core.exceptions import ValidationError
from django.db import transaction
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, rule, run_state_machine_as_test

from apps.access.exceptions import AccessDenied, NotInScope
from apps.handovers import services as handovers
from apps.handovers.services import HandoverLineInput, Receiver
from apps.material_requests import services as requests
from apps.material_requests.services import LineEdit
from apps.purchasing import services as purchasing
from apps.purchasing.services import AllocationAmendment, LineAmendment, RequestItem
from apps.receiving import services as receiving
from apps.receiving.services import ReceiptLineInput
from tests.invariants import (
    CLOSED,
    DRAFT,
    ISSUED,
    POSTED,
    Derived,
    Row,
    Rows,
    Watch,
    derive,
    is_live,
    is_open,
)
from tests.ledger_world import LedgerWorld, build_ledger_world
from tests.org import Org, People
from tests.support import principal_for

pytestmark = [pytest.mark.integration, pytest.mark.django_db]

D = Decimal
EXAMPLES = int(os.environ.get("LEDGER_MACHINE_EXAMPLES", "20"))
STEPS = int(os.environ.get("LEDGER_MACHINE_STEPS", "30"))
SETTINGS = settings(
    max_examples=EXAMPLES,
    stateful_step_count=STEPS,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

INDEX = st.integers(min_value=0, max_value=31)
QTY = st.integers(min_value=1, max_value=12)
DELTA = st.integers(min_value=-6, max_value=6).filter(bool)
SHIP_TO = st.sampled_from(["A Madrid", "B Madrid", "site"])
WAREHOUSE = st.sampled_from(["A Madrid", "B Madrid", "A Sevilla"])
PRIORITY = st.sampled_from(["LOW", "NORMAL", "HIGH", "URGENT"])
NEEDED_BY = st.none() | st.integers(min_value=0, max_value=20)
SPLIT = st.none() | st.tuples(INDEX, QTY)
RECEIVER = st.sampled_from(["technician", "partner"])
START = date(2026, 12, 1)

type Keep = Callable[[Row], bool]


class LedgerMachine(RuleBasedStateMachine):
    world: ClassVar[LedgerWorld]

    def __init__(self) -> None:
        super().__init__()
        self.savepoint = transaction.atomic()
        self.savepoint.__enter__()
        self.watch = Watch(self.world.ctx)
        self._derived: tuple[Rows, Derived] | None = None

    def teardown(self) -> None:
        transaction.set_rollback(True)
        self.savepoint.__exit__(None, None, None)

    # --- the state a step starts from ---------------------------------------------------

    @property
    def rows(self) -> Rows:
        """The rows read after the previous step."""
        return self.watch.rows

    @property
    def derived(self) -> Derived:
        """The reference ledger of ``rows`` (computed once per step)."""
        if self._derived is None or self._derived[0] is not self.rows:
            self._derived = (self.rows, derive(self.rows))
        return self._derived[1]

    def order_of(self, po_line_id: UUID) -> Row:
        return self.rows.orders[self.rows.order_lines[po_line_id]["po_id"]]

    def choose(self, table: str, index: int, keep: Keep | None = None) -> UUID | None:
        """The ``index``-th row (wrapping, in creation order) that ``keep`` accepts, or of
        all rows when none does, so a step can still be refused."""
        rows: dict[UUID, Row] = getattr(self.rows, table)
        ids = sorted(pk for pk, row in rows.items() if keep is None or keep(row))
        ids = ids or sorted(rows)
        return ids[index % len(ids)] if ids else None

    def open_line(self, row: Row) -> bool:
        return is_open(self.rows, row)

    def on(self, status: str) -> Keep:
        """Order lines or allocations of an order in ``status``."""

        def keep(row: Row) -> bool:
            po_line: UUID = row.get("po_line_id") or row["id"]
            return bool(self.order_of(po_line)["status"] == status)

        return keep

    # --- one service call, checked ----------------------------------------------------

    def attempt(
        self,
        operation: Callable[[], object],
        name: str,
        refusals: tuple[type[Exception], ...] = (ValidationError,),
    ) -> bool:
        """Run one service call; check every rule; return whether it was accepted.

        A refusal of ``refusals`` (the validation errors; for a project receiver also the
        access errors) must have written nothing."""
        try:
            with transaction.atomic():
                operation()
        except refusals:
            accepted = False
        else:
            accepted = True
        found = self.watch.step(refused=not accepted)
        assert not found, f"after {name}: " + "; ".join(str(item) for item in found)
        return accepted

    @property
    def buyer(self) -> Any:
        return self.world.buyer

    def location(self, place: str, project_id: UUID | None = None) -> UUID:
        if place == "site":
            assert project_id is not None
            return self.world.worksite(project_id).pk
        return self.world.warehouses[place].pk

    # --- purchasing ---------------------------------------------------------------------

    def to_purchase(self, line_id: UUID) -> Decimal:
        """The line's *to purchase* before the step, from the reference ledger."""
        row = self.rows.lines[line_id]
        if not is_open(self.rows, row) or row["sourcing"] != "PURCHASE":
            return D(0)
        f = self.derived.of_request_line(self.rows, line_id)
        committed = max(f.delivered, f.received) + f.pending + f.reserved
        return max(D(0), (row["approved_qty"] or D(0)) - committed)

    def raising(
        self, line_id: UUID, increase: Decimal, operation: Callable[[], object], name: str
    ) -> bool:
        """C5: an allocation raised beyond its line's *to purchase* is refused (the
        reconciliation after it would otherwise trim the excess away unnoticed)."""
        beyond = increase > self.to_purchase(line_id)
        accepted = self.attempt(operation, name)
        assert not (beyond and accepted), f"{name}: {increase} raised beyond to purchase"
        return accepted

    def buy(self, line_id: UUID, qty: int, ship_to: str) -> bool:
        org, project = self.world.org, self.rows.lines[line_id]["project_id"]

        def run() -> None:
            order = purchasing.create_po(
                self.buyer,
                branch_id=org.a_mad.pk,
                supplier_id=org.supplier.pk,
                ship_to_id=self.location(ship_to, project),
            )
            purchasing.add_request_lines(self.buyer, order.pk, [RequestItem(line_id, D(qty))])

        return self.raising(line_id, D(qty), run, "buy")

    def add_lines(self, po_id: UUID, line_id: UUID, qty: int) -> bool:
        items = [RequestItem(line_id, D(qty))]
        return self.raising(
            line_id, D(qty), lambda: purchasing.add_request_lines(self.buyer, po_id, items), "add"
        )

    def allocate(self, po_line_id: UUID, line_id: UUID, qty: int) -> bool:
        return self.raising(
            line_id,
            D(qty),
            lambda: purchasing.allocate(
                self.buyer, po_line_id, request_line_id=line_id, qty=D(qty)
            ),
            "allocate",
        )

    def raise_allocation(self, allocation_id: UUID, qty: Decimal) -> bool:
        row = self.rows.allocations[allocation_id]
        return self.raising(
            row["request_line_id"],
            qty - row["qty"],
            lambda: purchasing.increase_allocation(self.buyer, allocation_id, qty=qty),
            "increase",
        )

    def unallocate(self, allocation_id: UUID, qty: Decimal) -> bool:
        return self.attempt(
            lambda: purchasing.unallocate(self.buyer, allocation_id, qty=qty), "unallocate"
        )

    def set_ordered(self, po_line_id: UUID, qty: Decimal) -> bool:
        return self.attempt(
            lambda: purchasing.update_line(self.buyer, po_line_id, ordered_qty=qty), "ordered"
        )

    def issue(self, po_id: UUID) -> bool:
        return self.attempt(lambda: purchasing.issue(self.buyer, po_id), "issue")

    def amend(self, po_id: UUID, **changes: Any) -> bool:
        """Amend an issued order; a raised or added allocation is held to C5 too."""
        run = lambda: purchasing.amend(self.buyer, po_id, reason="Supplier", **changes)  # noqa: E731
        raised = [
            (
                self.rows.allocations[a.allocation_id]["request_line_id"],
                a.qty - self.rows.allocations[a.allocation_id]["qty"],
            )
            for a in changes.get("allocations", ())
        ] + [(item.request_line_id, item.qty) for item in changes.get("add", ())]
        raised = [(line, increase) for line, increase in raised if increase > 0]
        if not raised:
            return self.attempt(run, "amend")
        (line_id, increase), *_ = raised
        return self.raising(line_id, increase, run, "amend")

    def cancel(self, po_id: UUID) -> bool:
        return self.attempt(lambda: purchasing.cancel(self.buyer, po_id, reason="Stop"), "cancel")

    def close_order_line(self, po_line_id: UUID) -> bool:
        return self.attempt(
            lambda: purchasing.close_line_short(self.buyer, po_line_id, reason="Out of stock"),
            "close order line",
        )

    # --- receiving and handovers --------------------------------------------------------

    def _receivable_here(self, po_line_id: UUID, qty: int) -> bool:
        """At A-MAD's warehouse: the order is issued and the line can take ``qty`` (I4)."""
        rows, derived = self.rows, self.derived
        issued = self.order_of(po_line_id)["status"] == ISSUED
        capacity = derived.has_capacity(rows, po_line_id)
        return issued and capacity and qty <= derived.remaining(rows, po_line_id)

    def receive(
        self, po_line_id: UUID, location_id: UUID, qty: int, split: dict[UUID, Decimal] | None
    ) -> bool:
        madrid = location_id == self.world.warehouses["A Madrid"].pk and split is None
        expected = self._receivable_here(po_line_id, qty) if madrid else None
        item = ReceiptLineInput(po_line_id, D(qty), split=split)
        accepted = self.attempt(
            lambda: receiving.post_receipt(
                self.buyer, self.order_of(po_line_id)["id"], location_id=location_id, lines=[item]
            ),
            "receive",
        )
        if expected is not None:
            assert accepted == expected, f"a receipt of {qty} at A Madrid: expected {expected}"
        return accepted

    def receiver_projects(self, who: str) -> set[UUID]:
        """The worksites a project receiver reaches (Q44): A-MAD's technician the three
        projects A-MAD buys for or runs, the partner the hospital they are assigned to."""
        projects = self.world.projects
        return {p.pk for p in (projects if who == "technician" else projects[:1])}

    def receive_as(self, who: str, po_line_id: UUID, project_id: UUID, qty: int) -> bool:
        """A technician or partner posts a receipt at ``project_id``'s worksite, whoever
        requested the goods. Outside their projects it is refused, whatever the order."""
        people = self.world.people
        principal = principal_for(people.tech_mad if who == "technician" else people.partner)
        site = self.location("site", project_id)
        item = ReceiptLineInput(po_line_id, D(qty))
        refusals = (ValidationError, AccessDenied, NotInScope)
        accepted = self.attempt(
            lambda: receiving.post_receipt(
                principal, self.order_of(po_line_id)["id"], location_id=site, lines=[item]
            ),
            f"receive as the {who}",
            refusals,
        )
        if project_id not in self.receiver_projects(who):
            assert not accepted, f"the {who} received at a worksite outside their projects"
        return accepted

    def void_receipt(self, receipt_id: UUID) -> bool:
        posted = self.rows.receipts[receipt_id]["status"] == POSTED
        accepted = self.attempt(
            lambda: receiving.void_receipt(self.buyer, receipt_id, reason="Wrong"), "void receipt"
        )
        assert accepted == posted, "a posted receipt can always be voided, and only once"
        return accepted

    def hand_over(self, line_id: UUID, qty: int, source: str) -> bool:
        row = self.rows.lines[line_id]
        outstanding = (row["approved_qty"] or 0) - self.derived.delivered[line_id]
        fits = is_open(self.rows, row) and qty <= outstanding
        accepted = self.attempt(
            lambda: handovers.create_handover(
                self.buyer,
                row["project_id"],
                source_location_id=self.location(source),
                lines=[HandoverLineInput(line_id, D(qty))],
                receiver=Receiver(name="Site crew"),
            ),
            "hand over",
        )
        if source == "A Madrid":
            assert accepted == fits, f"a handover of {qty}: expected {fits} (outstanding only)"
        return accepted

    def void_handover(self, handover_id: UUID) -> bool:
        row = self.rows.handovers[handover_id]
        voidable = row["status"] == POSTED and row["kind"] == "FROM_WAREHOUSE"
        accepted = self.attempt(
            lambda: handovers.void_handover(self.buyer, handover_id, reason="Wrong"),
            "void handover",
        )
        assert accepted == voidable, "only a posted warehouse handover is voided on its own"
        return accepted

    # --- requests -----------------------------------------------------------------------

    def close_line(self, line_id: UUID) -> bool:
        return self.attempt(
            lambda: requests.close_line_short(self.buyer, line_id, reason="Enough"), "close line"
        )

    def reopen_line(self, line_id: UUID) -> bool:
        return self.attempt(
            lambda: requests.reopen_line(self.buyer, line_id, reason="Needed"), "reopen line"
        )

    def close_request(self, request_id: UUID) -> bool:
        return self.attempt(
            lambda: requests.manual_close(self.buyer, request_id, reason="Done"), "close request"
        )

    def reopen_request(self, request_id: UUID) -> bool:
        before = self.rows.lines
        accepted = self.attempt(
            lambda: requests.reopen(self.buyer, request_id, reason="Not done"), "reopen request"
        )
        if accepted:  # closures of single lines stay; the request's own are cleared
            for line_id, line in before.items():
                if line["request_id"] == request_id and line["closed_short_at"] is not None:
                    still = self.rows.lines[line_id]["closed_short_at"] is not None
                    assert still == (line["closed_short_source"] == "LINE"), line_id
        return accepted

    def reschedule(self, request_id: UUID, priority: str, needed_by: int | None) -> bool:
        day = None if needed_by is None else START + timedelta(days=needed_by)
        return self.attempt(
            lambda: requests.change_schedule(
                self.buyer, request_id, priority=priority, needed_by=day
            ),
            "reschedule",
        )

    def reduce(self, line_id: UUID, qty: Decimal) -> bool:
        return self.attempt(
            lambda: requests.reduce_approved_qty(self.buyer, line_id, approved_qty=qty, reason="-"),
            "reduce",
        )

    def source(self, line_id: UUID, sourcing: str) -> bool:
        return self.attempt(
            lambda: requests.set_sourcing(self.buyer, line_id, sourcing=sourcing), "source"
        )

    def edit(self, line_id: UUID, changes: dict[str, Any]) -> bool:
        edit = LineEdit("Reviewed", changes)
        return self.attempt(lambda: requests.edit_line(self.buyer, line_id, edit), "edit")

    # --- the rules hypothesis draws from ------------------------------------------------

    @initialize(start=st.sampled_from(["nothing", "warehouse", "site and draft"]))
    def start(self, start: str) -> None:
        """Begin with nothing, one issued order to A-MAD's warehouse for every line to
        purchase, or one to the hospital's site for X and Y and a draft for Z and W."""
        lines = self.world.lines
        if start == "nothing":
            return
        first = {"warehouse": "XYZW", "site and draft": "XY"}[start]
        place = "A Madrid" if start == "warehouse" else "site"
        assert self.buy(lines[first[0]].pk, 10, place)
        po_id = max(self.rows.orders)
        for name in first[1:]:
            assert self.add_lines(po_id, lines[name].pk, 6)
        assert self.issue(po_id)
        if start == "site and draft":
            assert self.buy(lines["Z"].pk, 6, "A Madrid")
            assert self.add_lines(max(self.rows.orders), lines["W"].pk, 7)

    @rule(line=INDEX, qty=QTY, ship_to=SHIP_TO)
    def rule_buy(self, line: int, qty: int, ship_to: str) -> None:
        to_buy = self.choose(
            "lines", line, lambda row: self.open_line(row) and row["sourcing"] == "PURCHASE"
        )
        if to_buy:
            self.buy(to_buy, qty, ship_to)

    @rule(
        kind=st.sampled_from(["add", "allocate", "ordered"]),
        which=INDEX,
        line=INDEX,
        qty=QTY,
    )
    def rule_change_draft(self, kind: str, which: int, line: int, qty: int) -> None:
        """Add a request line to a draft, allocate a draft line to one, or reorder."""
        open_line = self.choose("lines", line, self.open_line)
        if kind == "add":
            draft = self.choose("orders", which, lambda row: row["status"] == DRAFT)
            if draft and open_line:
                self.add_lines(draft, open_line, qty)
            return
        po_line = self.choose("order_lines", which, self.on(DRAFT))
        if po_line and kind == "ordered":
            self.set_ordered(po_line, D(qty))
        elif po_line and open_line:
            self.allocate(po_line, open_line, qty)

    @rule(raise_it=st.booleans(), which=INDEX, qty=QTY)
    def rule_change_draft_allocation(self, raise_it: bool, which: int, qty: int) -> None:
        """Raise a draft allocation (C5) or lower it (a trim)."""
        allocation = self.choose("allocations", which, self.on(DRAFT))
        if allocation is None:
            return
        current = self.rows.allocations[allocation]["qty"]
        if raise_it:
            self.raise_allocation(allocation, current + qty)
        else:
            self.unallocate(allocation, max(D(0), current - qty))

    @rule(order=INDEX)
    def rule_issue(self, order: int) -> None:
        draft = self.choose("orders", order, lambda row: row["status"] == DRAFT)
        if draft:
            self.issue(draft)

    @rule(
        kind=st.sampled_from(["ordered", "allocation", "add"]),
        which=INDEX,
        line=INDEX,
        delta=DELTA,
    )
    def rule_amend(self, kind: str, which: int, line: int, delta: int) -> None:
        if kind == "ordered":
            po_line = self.choose("order_lines", which, self.on(ISSUED))
            if po_line:
                qty = max(D(1), self.rows.order_lines[po_line]["ordered_qty"] + delta)
                self.amend(self.order_of(po_line)["id"], lines=[LineAmendment(po_line, qty)])
        elif kind == "allocation":
            allocation = self.choose("allocations", which, self.on(ISSUED))
            if allocation:
                row = self.rows.allocations[allocation]
                change = AllocationAmendment(allocation, max(D(0), row["qty"] + delta))
                self.amend(self.order_of(row["po_line_id"])["id"], allocations=[change])
        else:
            order = self.choose("orders", which, lambda row: row["status"] == ISSUED)
            open_line = self.choose("lines", line, self.open_line)
            if order and open_line:
                self.amend(order, add=[RequestItem(open_line, D(abs(delta)))])

    @rule(order_line=INDEX, qty=QTY, warehouse=WAREHOUSE, split=SPLIT)
    def rule_receive_at_a_warehouse(
        self, order_line: int, qty: int, warehouse: str, split: tuple[int, int] | None
    ) -> None:
        po_line = self.choose("order_lines", order_line, self.on(ISSUED))
        if po_line is None:
            return
        shares = None
        if split is not None:
            mine = {pk: a for pk, a in self.rows.allocations.items() if a["po_line_id"] == po_line}
            ids = sorted(mine)
            shares = {ids[split[0] % len(ids)]: D(split[1])} if ids else {}
        self.receive(po_line, self.location(warehouse), qty, shares)

    @rule(order_line=INDEX, qty=QTY, project=INDEX)
    def rule_receive_on_site(self, order_line: int, qty: int, project: int) -> None:
        """At the worksite of a project the line is allocated to (or of any project)."""
        po_line = self.choose("order_lines", order_line, self.on(ISSUED))
        if po_line is None:
            return
        served = sorted(
            {
                self.rows.lines[a["request_line_id"]]["project_id"]
                for a in self.rows.allocations.values()
                if a["po_line_id"] == po_line
            }
        ) or [p.pk for p in self.world.projects]
        site = self.location("site", served[project % len(served)])
        self.receive(po_line, site, qty, None)

    @rule(order_line=INDEX, qty=QTY, project=INDEX, who=RECEIVER)
    def rule_receive_as_a_project_receiver(
        self, order_line: int, qty: int, project: int, who: str
    ) -> None:
        """A technician or a partner receives at a worksite (Q44): no authorship of the
        request, a refusal outside their projects, the same ledger rules as anyone's."""
        po_line = self.choose("order_lines", order_line, self.on(ISSUED))
        if po_line is None:
            return
        projects = self.world.projects
        self.receive_as(who, po_line, projects[project % len(projects)].pk, qty)

    @rule(line=INDEX, qty=QTY, source=WAREHOUSE)
    def rule_hand_over(self, line: int, qty: int, source: str) -> None:
        line_id = self.choose("lines", line, self.open_line)
        if line_id:
            self.hand_over(line_id, qty, source)

    @rule(kind=st.sampled_from(["receipt", "handover"]), which=INDEX)
    def rule_void(self, kind: str, which: int) -> None:
        posted: Keep = lambda row: row["status"] == POSTED  # noqa: E731
        if kind == "receipt" and (receipt := self.choose("receipts", which, posted)):
            self.void_receipt(receipt)
        elif kind == "handover" and (handover := self.choose("handovers", which, posted)):
            self.void_handover(handover)

    @rule(kind=st.sampled_from(["line", "order line", "request", "order"]), which=INDEX)
    def rule_give_up(self, kind: str, which: int) -> None:
        """Close a request line, an order line or a request short, or cancel an order."""
        issued, approved = self.on(ISSUED), lambda row: row["status"] == "APPROVED"
        if kind == "line" and (line := self.choose("lines", which, is_live)):
            self.close_line(line)
        elif kind == "order line" and (po_line := self.choose("order_lines", which, issued)):
            self.close_order_line(po_line)
        elif kind == "request" and (request := self.choose("requests", which, approved)):
            self.close_request(request)
        elif kind == "order" and (order := self.choose("orders", which)):
            self.cancel(order)

    @rule(kind=st.sampled_from(["line", "request"]), which=INDEX)
    def rule_reopen(self, kind: str, which: int) -> None:
        closed_short: Keep = lambda row: row["closed_short_at"] is not None  # noqa: E731
        if kind == "line" and (line := self.choose("lines", which, closed_short)):
            self.reopen_line(line)
        elif kind == "request" and (
            request := self.choose("requests", which, lambda row: row["status"] == CLOSED)
        ):
            self.reopen_request(request)

    @rule(request=INDEX, priority=PRIORITY, needed_by=NEEDED_BY)
    def rule_reschedule(self, request: int, priority: str, needed_by: int | None) -> None:
        approved = self.choose("requests", request, lambda row: row["status"] == "APPROVED")
        if approved:
            self.reschedule(approved, priority, needed_by)

    @rule(line=INDEX, by=st.integers(min_value=1, max_value=4))
    def rule_reduce(self, line: int, by: int) -> None:
        live = self.choose("lines", line, is_live)
        if live:
            self.reduce(live, max(D(0), (self.rows.lines[live]["approved_qty"] or D(0)) - by))

    @rule(line=INDEX, purchase=st.booleans())
    def rule_source(self, line: int, purchase: bool) -> None:
        live = self.choose("lines", line, is_live)
        if live:
            self.source(live, "PURCHASE" if purchase else "STOCK")

    @rule(line=INDEX, material=st.booleans(), delta=DELTA)
    def rule_edit(self, line: int, material: bool, delta: int) -> None:
        live = self.choose("lines", line, is_live)
        if live is None:
            return
        row = self.rows.lines[live]
        if material:
            cable, cable4 = self.world.materials
            other = cable4 if row["material_id"] == cable.pk else cable
            self.edit(live, {"material_id": other.pk})
        else:
            self.edit(live, {"requested_qty": max(D(1), row["requested_qty"] + delta)})


@pytest.fixture
def world(org: Org, people: People) -> LedgerWorld:
    return build_ledger_world(org, people)


def machine_for(world: LedgerWorld) -> type[LedgerMachine]:
    return type("Ledger", (LedgerMachine,), {"world": world})


def test_the_ledger_holds_through_any_sequence(world: LedgerWorld) -> None:
    run_state_machine_as_test(machine_for(world), settings=SETTINGS)


def newest(m: LedgerMachine, table: str) -> UUID:
    ids: list[UUID] = sorted(getattr(m.rows, table))
    return ids[-1]


def allocation_of(m: LedgerMachine, name: str) -> UUID:
    line = m.world.lines[name].pk
    return next(pk for pk, a in m.rows.allocations.items() if a["request_line_id"] == line)


def names(m: LedgerMachine) -> tuple[dict[str, UUID], dict[str, UUID]]:
    """The world's lines and their requests, by line name."""
    lines = m.world.lines
    return {n: row.pk for n, row in lines.items()}, {n: row.request_id for n, row in lines.items()}


def site_of(m: LedgerMachine, project: int) -> UUID:
    return m.world.worksite(m.world.projects[project].pk).pk


def every_operation(m: LedgerMachine) -> None:
    """Every operation once, accepted and checked, in an order that keeps the next one
    possible: the random runs may draw an operation rarely, or only where it is refused."""
    line, request = names(m)
    world, madrid, site = m.world, m.world.warehouses["A Madrid"].pk, site_of(m, 1)

    assert m.buy(line["X"], 10, "A Madrid")
    po = newest(m, "orders")
    assert m.add_lines(po, line["Y"], 8)
    po_line = newest(m, "order_lines")  # X and Y share the cable line
    assert m.set_ordered(po_line, D(20))
    assert m.allocate(po_line, line["Z"], 2)
    assert m.unallocate(allocation_of(m, "Z"), D(1))
    assert m.raise_allocation(allocation_of(m, "Z"), D(2))
    assert m.issue(po)
    assert m.receive(po_line, madrid, 6, None)  # Y (HIGH) first
    assert m.receive(po_line, site, 3, None)  # Z 2 and 1 site surplus, handed over at once
    on_site = newest(m, "receipts")
    assert not m.void_handover(newest(m, "handovers"))  # a direct one goes with its receipt
    assert m.receive(po_line, madrid, 2, {allocation_of(m, "X"): D(2)})  # a manual split
    assert m.hand_over(line["X"], 2, "A Madrid")
    assert m.void_handover(newest(m, "handovers"))
    assert m.void_receipt(on_site)  # and its direct handover
    assert m.amend(po, lines=[LineAmendment(po_line, D(22))])
    assert m.amend(po, allocations=[AllocationAmendment(allocation_of(m, "Z"), D(4))])
    assert m.amend(po, add=[RequestItem(line["W"], D(2))])
    assert m.reschedule(request["Z"], "URGENT", 5)
    assert m.reduce(line["W"], D(6))
    assert m.source(line["S"], "PURCHASE")
    assert m.source(line["S"], "STOCK")
    cable, cable4 = world.materials
    assert m.edit(line["Z"], {"requested_qty": D(7)})
    assert m.edit(line["V"], {"material_id": cable.pk})  # in place: nothing allocated
    assert m.edit(line["Y"], {"material_id": cable4.pk})  # replaced: Y has received
    assert m.close_order_line(po_line)
    assert m.close_line(line["X"])
    assert m.reopen_line(line["X"])
    assert m.close_request(request["W"])
    assert m.reopen_request(request["W"])
    assert m.buy(line["W"], 2, "site")  # bought again, for SEV-2's site
    assert m.cancel(newest(m, "orders"))


def starved(m: LedgerMachine) -> UUID:
    """An order line of 23 for X 10, W 7 (urgent) and Z 6; 13 arrive at the hospital's
    site: X takes 10 and 3 are site surplus. Of the 10 left W pends 7 and Z only 3, so Z's
    ceiling (6) is above its pending. Return the order line."""
    line, request = names(m)
    assert m.reschedule(request["W"], "URGENT", None)
    assert m.buy(line["X"], 10, "A Madrid")
    po = newest(m, "orders")
    assert m.add_lines(po, line["W"], 7)
    assert m.add_lines(po, line["Z"], 6)
    assert m.issue(po)
    po_line = newest(m, "order_lines")
    assert m.receive(po_line, site_of(m, 0), 13, None)
    assert m.derived.pending[allocation_of(m, "Z")] == 3
    return po_line


def receipt_capacity_across_orders(m: LedgerMachine) -> None:
    """E13: Z's missing 3 are bought again and arrive; then 6 of the first order arrive at
    Z's site. Z may take only A - R = 3 of them (3 are site surplus): R never passes A."""
    line, _ = names(m)
    po_line = starved(m)
    assert m.buy(line["Z"], 3, "A Madrid")
    replacement = newest(m, "orders")
    assert m.issue(replacement)
    assert m.receive(newest(m, "order_lines"), m.world.warehouses["A Madrid"].pk, 3, None)
    assert m.receive(po_line, site_of(m, 1), 6, None)
    assert m.derived.attributed[allocation_of(m, "Z")] == 3


def site_receipt_after_stock(m: LedgerMachine) -> None:
    """E12 with untracked stock: Z gets 3 from the warehouse before anything arrived for it
    (D 3 + P 3 = A); then 6 arrive at Z's site. Z may take only A - D = 3 of them, whatever
    its ceiling (6): D never passes A."""
    line, _ = names(m)
    po_line = starved(m)
    assert m.hand_over(line["Z"], 3, "A Madrid")
    assert m.receive(po_line, site_of(m, 1), 6, None)
    assert m.derived.delivered[line["Z"]] == 6


def void_reopens_and_reconciles(m: LedgerMachine) -> None:
    """E8: an order line of 14 for Y 8 and Z 6 arrives whole at the hospital's site: Y
    takes 8 and is handed them (its request closes), 6 are site surplus, the order closes,
    and Z's 6 are bought again. Voiding the receipt reopens the order and Y's request, and
    the pending it revives for Z on the first order is cut (Z keeps its replacement)."""
    line, request = names(m)
    assert m.buy(line["Y"], 8, "A Madrid")
    po = newest(m, "orders")
    assert m.add_lines(po, line["Z"], 6)
    assert m.issue(po)
    assert m.receive(newest(m, "order_lines"), site_of(m, 0), 14, None)
    assert (m.rows.orders[po]["status"], m.rows.requests[request["Y"]]["status"]) == (
        "CLOSED",
        "CLOSED",
    )
    assert m.buy(line["Z"], 6, "A Madrid")
    assert m.issue(newest(m, "orders"))
    assert m.void_receipt(newest(m, "receipts"))
    assert (m.rows.orders[po]["status"], m.rows.requests[request["Y"]]["status"]) == (
        "ISSUED",
        "APPROVED",
    )
    first_z = next(
        pk
        for pk, a in m.rows.allocations.items()
        if a["request_line_id"] == line["Z"] and m.order_of(a["po_line_id"])["id"] == po
    )
    assert m.rows.allocations[first_z]["qty"] == 0


def warehouse_handover_closes_and_reopens(m: LedgerMachine) -> None:
    """Y's 8 arrive at A-MAD's warehouse and are handed over: its request has nothing left
    to deliver and closes; voiding the handover reopens it (§4.9)."""
    line, request = names(m)
    madrid = m.world.warehouses["A Madrid"].pk
    assert m.buy(line["Y"], 8, "A Madrid")
    assert m.issue(newest(m, "orders"))
    assert m.receive(newest(m, "order_lines"), madrid, 8, None)
    assert m.hand_over(line["Y"], 8, "A Madrid")
    assert m.rows.requests[request["Y"]]["status"] == "CLOSED"
    assert m.void_handover(newest(m, "handovers"))
    assert m.rows.requests[request["Y"]]["status"] == "APPROVED"


def project_receivers(m: LedgerMachine) -> None:
    """Q44: the goods of the hospital's X and Y (requested by the technician) arrive at its
    worksite and the partner receives them: Y is served first (HIGH) and its request
    closes with the direct handover, 2 are site surplus. The partner is refused at MAD-2's
    worksite, the technician is not; the material manager voids and Y's request reopens."""
    line, request = names(m)
    hospital, mad_2 = m.world.projects[0].pk, m.world.projects[1].pk
    assert m.buy(line["Y"], 8, "A Madrid")
    po = newest(m, "orders")
    assert m.add_lines(po, line["Z"], 6)
    assert m.issue(po)
    po_line = newest(m, "order_lines")
    assert m.receive_as("partner", po_line, hospital, 10)  # Y 8, 2 surplus on site
    assert m.rows.requests[request["Y"]]["status"] == "CLOSED"
    on_site = newest(m, "receipts")
    assert not m.receive_as("partner", po_line, mad_2, 2)  # not their project
    assert m.receive_as("technician", po_line, mad_2, 4)  # Z, requested by themselves too
    assert not m.void_handover(newest(m, "handovers"))  # a direct one goes with its receipt
    assert m.void_receipt(on_site)
    assert m.rows.requests[request["Y"]]["status"] == "APPROVED"


TOURS: dict[str, Callable[[LedgerMachine], None]] = {
    "project-receivers": project_receivers,
    "every-operation": every_operation,
    "receipt-capacity-across-orders": receipt_capacity_across_orders,
    "site-receipt-after-stock": site_receipt_after_stock,
    "void-reopens-and-reconciles": void_reopens_and_reconciles,
    "warehouse-handover-closes-and-reopens": warehouse_handover_closes_and_reopens,
}


@pytest.mark.parametrize("tour", TOURS.values(), ids=TOURS.keys())
def test_a_tour(world: LedgerWorld, tour: Callable[[LedgerMachine], None]) -> None:
    """Fixed sequences through the same checked operations: every operation at least
    once, and the shapes a cap exists for, which random walks seldom build."""
    machine = machine_for(world)()
    try:
        tour(machine)
    finally:
        machine.teardown()
