"""The allocation component: discovery, locking, the fixpoint and trims (plan §4.5, §4.7).

The worked examples (``test_examples``) show the protocol end to end; these tests take its
parts one by one, and check the fixpoint as a property: whatever the book, it ends with
every open line within its approved quantity, never raises a ceiling and never cuts below
what is attributed.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from django.core.exceptions import ValidationError
from django.db import transaction
from hypothesis import given, settings
from hypothesis import strategies as st

from apps.access.principals import SessionPrincipal
from apps.audit.models import AuditEvent
from apps.core.locking import LockLevel, LockOrder, LockSetChanged
from apps.material_requests import commitments
from apps.material_requests import services as request_services
from apps.material_requests.models import MaterialRequest, MaterialRequestLine
from apps.purchasing import components
from apps.purchasing.components import (
    Component,
    Seeds,
    TrimReason,
    discover,
    fixpoint,
    over_committed,
)
from apps.purchasing.models import PurchaseAllocation, PurchaseOrder
from apps.purchasing.waterfall import AllocationFacts, Book, OrderLineFacts, RequestLineFacts
from tests import orders
from tests.org import Org
from tests.support import error_codes
from tests.unit.purchasing.conftest import Buy, FakeReceipts, MakeLine, allocation, only_line

D = Decimal
T0 = datetime(2026, 10, 1, tzinfo=UTC)


@pytest.fixture
def chain(make_line: MakeLine, buy: Buy) -> tuple[list[MaterialRequestLine], list[PurchaseOrder]]:
    """r1 —o1— r2 —o2— r3: three request lines linked through two orders."""
    r1, r2, r3 = make_line("10"), make_line("10"), make_line("10")
    o1 = buy((r1, "5"), (r2, "5"))
    o2 = buy((r2, "5"), (r3, "5"))
    return [r1, r2, r3], [o1, o2]


@pytest.mark.django_db
class TestDiscovery:
    def test_through_every_allocation_both_ways(
        self, chain: tuple[list[MaterialRequestLine], list[PurchaseOrder]]
    ) -> None:
        (r1, r2, r3), (o1, o2) = chain
        nodes = discover(Seeds(request_lines=frozenset({r1.pk})))
        assert nodes.request_line_ids == {r1.pk, r2.pk, r3.pk}
        assert nodes.order_line_ids == {only_line(o1).pk, only_line(o2).pk}
        assert nodes.request_ids == {r1.request_id, r2.request_id, r3.request_id}
        assert nodes.order_ids == {o1.pk, o2.pk}
        assert discover(Seeds(orders=frozenset({o2.pk}))) == nodes

    def test_a_header_seed_brings_all_its_lines(self, org: Org, make_line: MakeLine) -> None:
        line = make_line("10")
        draft = orders.order(org)
        nodes = discover(Seeds(requests=frozenset({line.request_id}), orders=frozenset({draft.pk})))
        assert nodes.request_line_ids == {line.pk}
        assert nodes.order_ids == {draft.pk}
        assert nodes.order_line_ids == frozenset()

    def test_trimmed_and_cancelled_allocations_link_nothing(
        self, chain: tuple[list[MaterialRequestLine], list[PurchaseOrder]]
    ) -> None:
        (r1, r2, _r3), (o1, o2) = chain
        PurchaseAllocation.objects.filter(po_line__po=o2, request_line=r2).update(qty=0)
        assert discover(Seeds(request_lines=frozenset({r1.pk}))).order_ids == {o1.pk}
        PurchaseAllocation.objects.filter(po_line__po=o2, request_line=r2).update(qty=5)
        PurchaseOrder.objects.filter(pk=o2.pk).update(status="CANCELLED")
        assert discover(Seeds(request_lines=frozenset({r1.pk}))).order_ids == {o1.pk}

    def test_the_bound(
        self,
        chain: tuple[list[MaterialRequestLine], list[PurchaseOrder]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (r1, _r2, _r3), _orders = chain  # 5 nodes: 3 request lines, 2 order lines
        monkeypatch.setattr(components, "MAX_NODES", 5)
        discover(Seeds(request_lines=frozenset({r1.pk})))
        monkeypatch.setattr(components, "MAX_NODES", 4)
        with pytest.raises(ValidationError) as exc:
            discover(Seeds(request_lines=frozenset({r1.pk})))
        assert error_codes(exc) == {"component_too_large"}
        monkeypatch.setattr(components, "MAX_NODES", 1)  # the seeds alone are too many
        with pytest.raises(ValidationError):
            discover(Seeds(orders=frozenset({_orders[0].pk})))


@pytest.mark.django_db
class TestLocking:
    def test_locks_every_level_and_snapshots_pending(
        self, chain: tuple[list[MaterialRequestLine], list[PurchaseOrder]]
    ) -> None:
        (r1, r2, _r3), (o1, _o2) = chain
        with transaction.atomic():
            locks = LockOrder()
            component = Component.lock(locks, request_lines=[r1.pk])
            assert locks.level == LockLevel.PO_LINES
            book = component.book()
            assert component._before == {pk: book.pending(pk) for pk in book.allocations}
            assert component._before[allocation(o1, r2).pk] == 5

    def test_a_component_that_changes_while_locking_is_retried(
        self,
        chain: tuple[list[MaterialRequestLine], list[PurchaseOrder]],
        monkeypatch: pytest.MonkeyPatch,
        mm: SessionPrincipal,
    ) -> None:
        (r1, _r2, _r3), _orders = chain
        calls: list[Any] = []
        real = components.discover

        def shifting(seeds: Seeds) -> components.Nodes:
            calls.append(seeds)
            found = real(seeds)
            if len(calls) % 2 == 0:  # the second look, under the locks, sees another set
                return components.Nodes(
                    found.request_ids,
                    found.request_line_ids | {UUID(int=1)},
                    found.order_ids,
                    found.order_line_ids,
                )
            return found

        monkeypatch.setattr(components, "discover", shifting)
        with transaction.atomic(), pytest.raises(LockSetChanged):
            Component.lock(LockOrder(), request_lines=[r1.pk])
        # Inside the test's transaction the wrapper does not retry (savepoint); in a real
        # request it retries up to three times (``apps.core.locking``).
        with pytest.raises(LockSetChanged):
            request_services.change_priority(mm, r1.request_id, priority="LOW")
        assert MaterialRequest.objects.get(pk=r1.request_id).priority == "NORMAL"

    def test_the_dispatch_hook_runs_between_orders_and_lines(
        self, chain: tuple[list[MaterialRequestLine], list[PurchaseOrder]]
    ) -> None:
        _lines, (o1, _o2) = chain
        levels = []
        with transaction.atomic():
            Component.lock(
                LockOrder(),
                orders=[o1.pk],
                between_orders_and_lines=lambda held: levels.append(held.level),
            )
        assert levels == [LockLevel.PO_HEADERS]

    def test_rows_outside_the_component_are_refused(
        self,
        chain: tuple[list[MaterialRequestLine], list[PurchaseOrder]],
        make_line: MakeLine,
        buy: Buy,
        mm: SessionPrincipal,
    ) -> None:
        (r1, _r2, _r3), _orders = chain
        stranger = make_line("10")
        other = buy((stranger, "5"))
        with transaction.atomic():
            component = Component.lock(LockOrder(), request_lines=[r1.pk])
            outside = allocation(other, stranger)
            with pytest.raises(RuntimeError, match="outside the locked component"):
                component.trim(mm, outside, D(0), TrimReason.UNALLOCATED, "test")
            component.nodes = components.Nodes(
                component.nodes.request_ids,
                frozenset({r1.pk}),
                component.nodes.order_ids,
                component.nodes.order_line_ids,
            )
            with pytest.raises(RuntimeError, match="reaches outside"):
                component.book()


@pytest.mark.django_db
class TestTrim:
    def test_never_below_what_is_attributed_and_never_up(
        self,
        make_line: MakeLine,
        buy: Buy,
        received: FakeReceipts,
        mm: SessionPrincipal,
    ) -> None:
        line = make_line("10")
        order = buy((line, "10"))
        received.receive(only_line(order), "4", {allocation(order, line): "4"})
        with transaction.atomic():
            component = Component.lock(LockOrder(), orders=[order.pk])
            row = allocation(order, line)
            assert component.trim(mm, row, D(10), TrimReason.UNALLOCATED, "t") is None
            assert component.trim(mm, row, D(12), TrimReason.UNALLOCATED, "t") is None
            with pytest.raises(ValidationError) as exc:
                component.trim(mm, row, D(3), TrimReason.UNALLOCATED, "t")
            assert error_codes(exc) == {"below_attributed"}
            trim = component.trim(mm, row, D(4), TrimReason.UNALLOCATED, "t")
        assert trim == components.Trim(row.pk, D(10), D(4), TrimReason.UNALLOCATED)
        event = AuditEvent.objects.get(action="purchase_allocation.trimmed")
        assert event.object_id == row.pk and event.branch_id == order.branch_id
        assert event.data | {"order_id": None, "request_id": None} == {
            "order_id": None,
            "order": order.number,
            "order_line": 1,
            "request_id": None,
            "request": line.request.number,
            "request_line": 1,
            "from": "10.000",
            "to": "4",
            "reason": "UNALLOCATED",
            "cause": "t",
        }


# --- the fixpoint, pure --------------------------------------------------------------


def _facts(
    lines: list[tuple[int, str, str]],
    requests_: list[tuple[int, str]],
    allocations: list[tuple[int, int, int, str]],
) -> Book:
    """``lines``: (n, status, ordered); ``requests_``: (n, approved); ``allocations``:
    (n, line n, request n, qty)."""
    return Book(
        [
            OrderLineFacts(UUID(int=100 + n), UUID(int=200 + n), status, D(ordered), D(0), False)
            for n, status, ordered in lines
        ],
        [
            RequestLineFacts(UUID(int=300 + n), UUID(int=400 + n), D(a), True, "NORMAL", None, T0)
            for n, a in requests_
        ],
        [
            AllocationFacts(UUID(int=n), UUID(int=100 + line), UUID(int=300 + r), D(q), D(0))
            for n, line, r, q in allocations
        ],
    )


class TestFixpoint:
    def test_draft_reservations_go_first_newest_first(self) -> None:
        book = _facts(
            [(1, "ISSUED", "10"), (2, "DRAFT", "10"), (3, "DRAFT", "10")],
            [(1, "10")],
            [(1, 1, 1, "6"), (2, 2, 1, "3"), (3, 3, 1, "3")],
        )  # committed 12 of 10
        assert fixpoint(book, {}, {}) == {UUID(int=3): D(1)}

    def test_then_pending_that_rose_then_the_newest_pending(self) -> None:
        book = _facts(
            [(1, "ISSUED", "10"), (2, "ISSUED", "10"), (3, "ISSUED", "10")],
            [(1, "10")],
            [(1, 1, 1, "6"), (2, 2, 1, "3"), (3, 3, 1, "3")],
        )
        rose = {UUID(int=1): D(2), UUID(int=2): D(3), UUID(int=3): D(3)}
        assert fixpoint(book, {}, rose) == {UUID(int=1): D(4)}
        assert fixpoint(book, {}, {UUID(int=1): D(6), UUID(int=2): D(3), UUID(int=3): D(3)}) == {
            UUID(int=3): D(1)
        }

    def test_delivered_counts_and_an_unfixable_line_is_an_integrity_error(self) -> None:
        book = _facts([(1, "ISSUED", "10")], [(1, "10")], [(1, 1, 1, "5")])
        assert fixpoint(book, {UUID(int=301): D(7)}, {}) == {UUID(int=1): D(3)}
        with pytest.raises(ValidationError) as exc:
            fixpoint(book, {UUID(int=301): D(11)}, {})
        assert error_codes(exc) == {"integrity"}

    def test_it_gives_up_rather_than_loop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(components, "MAX_CUTS", 0)
        book = _facts([(1, "ISSUED", "10")], [(1, "1")], [(1, 1, 1, "5")])
        with pytest.raises(RuntimeError, match="did not converge"):
            fixpoint(book, {}, {})

    def test_over_committed_lines_in_attribution_order(self) -> None:
        book = _facts(
            [(1, "ISSUED", "10"), (2, "ISSUED", "10")],
            [(1, "1"), (2, "1"), (3, "9")],
            [(1, 1, 2, "5"), (2, 2, 1, "5"), (3, 2, 3, "5")],
        )
        assert [line.id for line in over_committed(book, {})] == [UUID(int=301), UUID(int=302)]


@st.composite
def unbalanced_books(draw: st.DrawFn) -> tuple[Book, dict[UUID, Decimal]]:
    count = draw(st.integers(1, 4))
    requests_ = [
        RequestLineFacts(
            UUID(int=300 + n),
            UUID(int=400 + n),
            D(draw(st.integers(0, 20))),
            draw(st.booleans()),
            draw(st.sampled_from(["LOW", "NORMAL", "URGENT"])),
            None,
            T0,
        )
        for n in range(count)
    ]
    lines: list[OrderLineFacts] = []
    allocations: list[AllocationFacts] = []
    for n in range(draw(st.integers(1, 4))):
        lines.append(
            OrderLineFacts(
                UUID(int=100 + n),
                UUID(int=200 + n),
                draw(st.sampled_from(["DRAFT", "ISSUED"])),
                D(draw(st.integers(1, 40))),
                D(0),
                False,
            )
        )
        for request in draw(st.lists(st.sampled_from(requests_), unique=True)):
            allocations.append(
                AllocationFacts(
                    UUID(int=len(allocations) + 1),
                    lines[-1].id,
                    request.id,
                    D(draw(st.integers(0, 25))),
                    D(0),
                )
            )
    delivered = {r.id: D(draw(st.integers(0, int(r.approved)))) for r in requests_}
    return Book(lines, requests_, allocations), delivered


@settings(max_examples=300, deadline=None)
@given(unbalanced_books(), st.data())
def test_the_fixpoint_restores_i2(case: tuple[Book, dict[UUID, Decimal]], data: Any) -> None:
    book, delivered = case
    before = {pk: D(data.draw(st.integers(0, 25))) for pk in book.allocations}
    cut = fixpoint(book, delivered, before)
    after = book.with_quantities(cut)
    assert over_committed(after, delivered) == []
    for pk, quantity in cut.items():
        allocation = book.allocations[pk]
        assert allocation.attributed <= quantity < allocation.qty


# --- the request services' seam ------------------------------------------------------


@pytest.mark.django_db
class TestCommitments:
    def test_purchasing_is_the_registered_locker(self) -> None:
        assert commitments._lockers == [components.lock_for_requests]
        commitments.register_component_locker(components.lock_for_requests)  # ready() again
        assert commitments._lockers == [components.lock_for_requests]
        with pytest.raises(ValueError, match="already registered"):
            commitments.register_component_locker(lambda locks, ids: None)  # type: ignore[arg-type,return-value]

    def test_without_purchasing_the_request_alone_is_locked(
        self,
        make_line: MakeLine,
        mm: SessionPrincipal,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(commitments, "_lockers", [])
        line = make_line("10")
        with transaction.atomic():
            locks = LockOrder()
            component = commitments.lock_component(locks, [line.request_id])
            assert locks.level == LockLevel.REQUEST_LINES
            component.trim_request_lines(mm, [line.pk], reason="X", cause="y")
            assert component.reconcile(mm, "y") == []
        request_services.close_line_short(mm, line.pk, reason="Without purchasing")
        assert MaterialRequest.objects.get(pk=line.request_id).status == "CLOSED"

    def test_registering_returns_its_undo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(commitments, "_lockers", [])
        locker: Callable[..., Any] = lambda locks, ids: None  # noqa: E731
        unregister = commitments.register_component_locker(locker)
        assert commitments._lockers == [locker]
        unregister()
        unregister()
        assert commitments._lockers == []
