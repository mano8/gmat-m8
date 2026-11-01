"""The purchasing services one by one: editing rules, C5, C6, C10, S9, idempotency, audit.

The access matrix of every service is ``test_matrix``; the worked examples are
``test_examples``. Each refusal is checked by its code (messages are translated).
"""

from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from django.core.exceptions import ValidationError
from django.db import transaction

from apps.access.principals import SessionPrincipal
from apps.audit.models import AuditEvent
from apps.catalog.models import Material
from apps.core.idempotency import IdempotencyConflict, issue_key
from apps.core.locking import LockOrder, LockSetChanged
from apps.locations.models import Location
from apps.material_requests.models import MaterialRequestLine
from apps.notifications.models import EmailOutbox, Kind
from apps.notifications.models import Status as MailStatus
from apps.organizations.models import Branch, Company
from apps.projects.models import Project
from apps.purchasing import services
from apps.purchasing.components import Component
from apps.purchasing.models import PurchaseAllocation, PurchaseOrder, PurchaseOrderLine
from apps.purchasing.services import (
    AllocationAmendment,
    HeaderAmendment,
    LineAmendment,
    RequestItem,
    common,
    orders,
)
from apps.suppliers.models import Supplier
from tests.org import Org, People
from tests.support import error_codes, principal_for
from tests.unit.purchasing.conftest import (
    Buy,
    FakeReceipts,
    MakeLine,
    allocation,
    flows,
    only_line,
)

pytestmark = pytest.mark.django_db

D = Decimal
Status = PurchaseOrder.Status


def codes(call: Any, *args: Any, **kwargs: Any) -> set[str]:
    with pytest.raises(ValidationError) as exc:
        call(*args, **kwargs)
    return error_codes(exc)


def actions() -> list[str]:
    return list(AuditEvent.objects.order_by("id").values_list("action", flat=True))


def reload(order: PurchaseOrder) -> PurchaseOrder:
    return PurchaseOrder.objects.get(pk=order.pk)


# --- create and edit a draft ----------------------------------------------------------


class TestCreate:
    def test_a_draft_of_the_branch_bought_by_its_company(
        self, org: Org, mm: SessionPrincipal
    ) -> None:
        order = services.create_po(
            mm,
            branch_id=org.a_mad.pk,
            supplier_id=org.supplier.pk,
            ship_to_id=org.warehouses["B Madrid"].pk,  # a warehouse of a managed branch
            expected_date=date(2026, 11, 15),
            supplier_ref="  OF-1  ",
            notes=" Urgent ",
        )
        assert (order.status, order.buyer_company_id, order.number) == (
            Status.DRAFT,
            org.a.pk,
            None,
        )
        assert (order.supplier_ref, order.notes) == ("OF-1", "Urgent")
        assert actions() == ["purchase_order.created"]

    def test_a_worksite_of_a_project_the_branch_buys_for(
        self, org: Org, mm: SessionPrincipal
    ) -> None:
        site = org.worksite(org.sev_for_mad)  # SEV-2: A-SEV's project, A-MAD buys
        order = services.create_po(
            mm, branch_id=org.a_mad.pk, supplier_id=org.supplier.pk, ship_to_id=site.pk
        )
        assert order.ship_to_id == site.pk

    def test_idempotent(self, org: Org, people: People, mm: SessionPrincipal) -> None:
        key = issue_key(people.mm_mad.pk, "purchase_order.create")
        values: dict[str, Any] = {
            "branch_id": org.a_mad.pk,
            "supplier_id": org.supplier.pk,
            "ship_to_id": org.warehouses["A Madrid"].pk,
            "idempotency_key": key,
        }
        first = services.create_po(mm, **values)
        assert services.create_po(mm, **values) == first
        with pytest.raises(IdempotencyConflict):
            services.create_po(mm, **(values | {"notes": "other"}))
        assert PurchaseOrder.objects.count() == 1

    @pytest.mark.parametrize(
        ("destination", "code"),
        [
            (lambda org: org.warehouses["A Sevilla"], "ship_to"),  # another branch's
            (lambda org: org.worksite(org.sev_works), "ship_to"),  # A-SEV buys for it
            (lambda org: org.worksite(org.mad_hold), "ship_to"),  # on hold
        ],
    )
    def test_invalid_destinations(
        self, org: Org, people: People, destination: Any, code: str
    ) -> None:
        admin = principal_for(people.group_admin)  # sees every branch: no 404 first
        assert codes(
            services.create_po,
            admin,
            branch_id=org.a_mad.pk,
            supplier_id=org.supplier.pk,
            ship_to_id=destination(org).pk,
        ) == {code}

    def test_inactive_destinations_suppliers_and_branches(
        self, org: Org, people: People, mm: SessionPrincipal
    ) -> None:
        values = {
            "branch_id": org.a_mad.pk,
            "supplier_id": org.supplier.pk,
            "ship_to_id": org.warehouses["A Madrid"].pk,
        }
        Location.objects.filter(pk=values["ship_to_id"]).update(is_active=False)
        assert codes(services.create_po, mm, **values) == {"ship_to"}
        Location.objects.filter(pk=values["ship_to_id"]).update(is_active=True)
        Supplier.objects.filter(pk=org.supplier.pk).update(is_active=False)
        assert codes(services.create_po, mm, **values) == {"inactive"}
        Supplier.objects.filter(pk=org.supplier.pk).update(is_active=True)
        # An inactive branch leaves the manager's scope (404); the administrator still sees
        # its warehouse and its projects' worksites, and the rule refuses them (S9).
        admin = principal_for(people.group_admin)
        Branch.objects.filter(pk=org.b_mad.pk).update(is_active=False)
        for location in (org.warehouses["B Madrid"], org.worksite(org.hospital)):
            assert codes(services.create_po, admin, **(values | {"ship_to_id": location.pk})) == {
                "ship_to"
            }
        # A-MAD's own manager loses the branch with its company (S9).
        Company.objects.filter(pk=org.a.pk).update(is_active=False)
        assert codes(services.create_po, admin, **values) == {"inactive_branch"}


class TestUpdateOrder:
    def test_a_draft_changes_its_header(self, org: Org, mm: SessionPrincipal, buy: Buy) -> None:
        order = buy(issue=False)
        other = Supplier.objects.create(name="Other supplies")
        updated = services.update_order(
            mm,
            order.pk,
            supplier_id=other.pk,
            ship_to_id=org.warehouses["B Madrid"].pk,
            notes=" Call first ",
            supplier_ref="",
        )
        assert (updated.supplier_id, updated.notes) == (other.pk, "Call first")
        assert services.update_order(mm, order.pk, notes="Call first") == updated  # no change
        assert actions()[-1] == "purchase_order.updated"
        assert actions().count("purchase_order.updated") == 1

    def test_rules(self, org: Org, mm: SessionPrincipal, buy: Buy, make_line: MakeLine) -> None:
        with pytest.raises(ValueError, match="cannot be changed here"):
            services.update_order(mm, buy(issue=False).pk, number="X")
        order = buy((make_line("10"), "5"), issue=False)  # for the hospital
        gone = Supplier.objects.create(name="Gone", is_active=False)
        assert codes(services.update_order, mm, order.pk, supplier_id=gone.pk) == {"inactive"}
        site = org.worksite(org.sev_for_mad)  # SEV-2's worksite: C6
        assert codes(services.update_order, mm, order.pk, ship_to_id=site.pk) == {
            "worksite_projects"
        }
        hospital = org.worksite(org.hospital)
        assert services.update_order(mm, order.pk, ship_to_id=hospital.pk).ship_to_id == hospital.pk
        issued = buy((make_line("10"), "5"))
        assert codes(services.update_order, mm, issued.pk, notes="x") == {"amend"}
        services.cancel(mm, issued.pk, reason="Not needed")
        assert codes(services.update_order, mm, issued.pk, notes="x") == {"transition"}


# --- lines and allocations of a draft ---------------------------------------------------


class TestAddRequestLines:
    def test_aggregates_per_material_and_defaults_to_what_is_left(
        self, mm: SessionPrincipal, buy: Buy, make_line: MakeLine, breaker: Material
    ) -> None:
        cable, other = make_line("10"), make_line("4", material=breaker)
        order = buy((cable, "3"), issue=False)
        services.add_request_lines(mm, order.pk, [RequestItem(cable.pk), RequestItem(other.pk)])
        lines = list(order.lines.order_by("line_no"))
        assert [(line.line_no, line.ordered_qty) for line in lines] == [(1, 10), (2, 4)]
        assert allocation(order, cable).qty == 10  # 3, then the 7 left
        assert lines[0].material_snapshot == {}  # taken at issue
        assert actions()[-4:] == [
            "purchase_order_line.increased",
            "purchase_allocation.increased",
            "purchase_order_line.added",
            "purchase_allocation.created",
        ]

    def test_arguments(self, mm: SessionPrincipal, buy: Buy, make_line: MakeLine) -> None:
        order, line = buy(issue=False), make_line("10")
        with pytest.raises(ValueError, match="once per call"):
            services.add_request_lines(mm, order.pk, [RequestItem(line.pk), RequestItem(line.pk)])
        assert codes(services.add_request_lines, mm, order.pk, []) == {"empty"}
        with pytest.raises(MaterialRequestLine.DoesNotExist):
            services.add_request_lines(mm, order.pk, [RequestItem(UUID(int=1))])
        assert codes(
            services.add_request_lines, mm, order.pk, [RequestItem(line.pk, D("0.001"))]
        ) == {"precision"}

    @pytest.mark.parametrize(
        ("change", "code"),
        [
            ({"status": "CLOSED"}, "not_open"),
            ({"sourcing": "STOCK"}, "not_purchase"),
            ({"project": "sev_works"}, "branch_mismatch"),
            ({"project_status": "ON_HOLD"}, "project_not_active"),
            ({"branch_inactive": True}, "inactive_branch"),
        ],
    )
    def test_c5_on_the_request_side(
        self,
        org: Org,
        people: People,
        buy: Buy,
        make_line: MakeLine,
        change: dict[str, Any],
        code: str,
    ) -> None:
        admin = principal_for(people.group_admin)  # every request is in scope
        if "project" in change:
            line = make_line("10", project=getattr(org, change["project"]))
        else:
            line = make_line(
                "10", **{k: v for k, v in change.items() if k in {"status", "sourcing"}}
            )
        if "project_status" in change:
            Project.objects.filter(pk=line.project_id).update(status=change["project_status"])
        if "branch_inactive" in change:
            Branch.objects.filter(pk=org.b_mad.pk).update(is_active=False)
        order = buy(issue=False)
        assert codes(services.add_request_lines, admin, order.pk, [RequestItem(line.pk)]) == {code}

    def test_c6_a_worksite_order_serves_its_project_only(
        self, org: Org, mm: SessionPrincipal, buy: Buy, make_line: MakeLine
    ) -> None:
        order = buy(issue=False, ship_to=org.worksite(org.sev_for_mad))
        line = make_line("10")  # the hospital's
        assert codes(services.add_request_lines, mm, order.pk, [RequestItem(line.pk)]) == {
            "worksite_projects"
        }
        own = make_line("10", project=org.sev_for_mad)
        services.add_request_lines(mm, order.pk, [RequestItem(own.pk)])

    def test_an_inactive_material_is_not_bought_anew(
        self, mm: SessionPrincipal, buy: Buy, make_line: MakeLine, cable: Material
    ) -> None:
        line = make_line("10")
        Material.objects.filter(pk=cable.pk).update(is_active=False)
        order = buy(issue=False)
        assert codes(services.add_request_lines, mm, order.pk, [RequestItem(line.pk)]) == {
            "inactive"
        }

    def test_idempotent_and_drafts_only(
        self, people: People, mm: SessionPrincipal, buy: Buy, make_line: MakeLine
    ) -> None:
        line = make_line("10")
        order = buy(issue=False)
        key = issue_key(people.mm_mad.pk, "purchase_order.add_request_lines")
        items = [RequestItem(line.pk, D(4))]
        services.add_request_lines(mm, order.pk, items, idempotency_key=key)
        services.add_request_lines(mm, order.pk, items, idempotency_key=key)  # a double tap
        assert allocation(order, line).qty == 4
        issued = buy((make_line("10"), "5"))
        assert codes(services.add_request_lines, mm, issued.pk, items) == {"amend"}


class TestAllocate:
    def test_part_of_a_line_to_another_request_line(
        self, mm: SessionPrincipal, buy: Buy, make_line: MakeLine, people: People
    ) -> None:
        first, second = make_line("10"), make_line("10")
        order = buy((first, "6"), issue=False)
        services.update_line(mm, only_line(order).pk, ordered_qty=D(10))
        key = issue_key(people.mm_mad.pk, "purchase_allocation.allocate")
        made = services.allocate(
            mm, only_line(order).pk, request_line_id=second.pk, qty=D(4), idempotency_key=key
        )
        again = services.allocate(
            mm, only_line(order).pk, request_line_id=second.pk, qty=D(4), idempotency_key=key
        )
        assert made == again and made.qty == 4
        assert flows(second).reserved == 4

    def test_rules(
        self, mm: SessionPrincipal, buy: Buy, make_line: MakeLine, breaker: Material
    ) -> None:
        first, second = make_line("10"), make_line("10")
        order = buy((first, "6"), issue=False)
        line = only_line(order)
        assert codes(services.allocate, mm, line.pk, request_line_id=first.pk, qty=D(1)) == {
            "allocated"
        }
        assert codes(services.allocate, mm, line.pk, request_line_id=second.pk, qty=D(1)) == {
            "exceeds_ordered"
        }
        units = make_line("3", material=breaker)
        assert codes(services.allocate, mm, line.pk, request_line_id=units.pk, qty=D(1)) == {
            "material_mismatch"
        }
        services.update_line(mm, line.pk, ordered_qty=D(20))
        assert codes(services.allocate, mm, line.pk, request_line_id=second.pk, qty=D(11)) == {
            "exceeds_to_purchase"
        }
        PurchaseOrderLine.objects.filter(pk=line.pk).update(
            closed_short_at=line.created_at, close_reason="x"
        )
        assert codes(services.allocate, mm, line.pk, request_line_id=second.pk, qty=D(1)) == {
            "closed_short"
        }


class TestIncreaseAndUnallocate:
    def test_raise_lower_and_no_ops(
        self, mm: SessionPrincipal, buy: Buy, make_line: MakeLine
    ) -> None:
        line = make_line("10")
        order = buy((line, "4"), issue=False)
        services.update_line(mm, only_line(order).pk, ordered_qty=D(10))
        row = allocation(order, line)
        assert services.increase_allocation(mm, row.pk, qty=D(4)).qty == 4  # unchanged
        assert codes(services.increase_allocation, mm, row.pk, qty=D(3)) == {"not_higher"}
        assert services.increase_allocation(mm, row.pk, qty=D(9)).qty == 9
        assert codes(services.increase_allocation, mm, row.pk, qty=D(11)) == {"exceeds_ordered"}
        assert services.unallocate(mm, row.pk, qty=D(9)).qty == 9  # unchanged
        assert codes(services.unallocate, mm, row.pk, qty=D(10)) == {"not_lower"}
        assert codes(services.unallocate, mm, row.pk, qty=D("-1")) == {"positive"}
        assert services.unallocate(mm, row.pk, qty=D(2)).qty == 2
        assert services.unallocate(mm, row.pk).qty == 0
        trims = AuditEvent.objects.filter(action="purchase_allocation.trimmed").order_by("id")
        assert [(t.data["from"], t.data["to"], t.data["reason"]) for t in trims] == [
            ("9.000", "2", "UNALLOCATED"),
            ("2.000", "0", "UNALLOCATED"),
        ]

    def test_drafts_only(self, mm: SessionPrincipal, buy: Buy, make_line: MakeLine) -> None:
        line = make_line("10")
        row = allocation(buy((line, "4")), line)
        assert codes(services.increase_allocation, mm, row.pk, qty=D(5)) == {"amend"}
        assert codes(services.unallocate, mm, row.pk) == {"amend"}


class TestUpdateAndRemoveLine:
    def test_quantities_and_prices(
        self, mm: SessionPrincipal, buy: Buy, make_line: MakeLine
    ) -> None:
        x, y = make_line("10"), make_line("10")
        order = buy((x, "6"), (y, "4"), issue=False)
        line = only_line(order)
        updated = services.update_line(
            mm, line.pk, unit_price=D("12.5"), discount_pct=D("10"), ordered_qty=D(7)
        )
        # Lowered below its allocations (10): the newest in the attribution order first.
        assert (updated.ordered_qty, updated.unit_price, updated.discount_pct) == (
            7,
            D("12.5"),
            D(10),
        )
        assert [allocation(order, item).qty for item in (x, y)] == [6, 1]
        assert services.update_line(mm, line.pk, ordered_qty=D(7)) == updated  # no change
        services.update_line(mm, line.pk, unit_price=None, ordered_qty=D(8))
        assert only_line(order).unit_price is None
        for price, code in ((D("-1"), "invalid"), ("12", "invalid")):
            assert codes(services.update_line, mm, line.pk, unit_price=price) == {code}
        for discount in (D(101), 5):
            assert codes(services.update_line, mm, line.pk, discount_pct=discount) == {"invalid"}
        assert codes(services.update_line, mm, line.pk, ordered_qty=D(0)) == {"positive"}

    def test_lowering_through_every_allocation(
        self, mm: SessionPrincipal, buy: Buy, make_line: MakeLine
    ) -> None:
        x, y = make_line("10"), make_line("10")
        order = buy((x, "6"), (y, "4"), issue=False)
        services.update_line(mm, only_line(order).pk, ordered_qty=D(1))
        assert [allocation(order, item).qty for item in (x, y)] == [1, 0]

    def test_remove_a_draft_line_and_its_reservations(
        self, mm: SessionPrincipal, buy: Buy, make_line: MakeLine
    ) -> None:
        line = make_line("10")
        order = buy((line, "4"), issue=False)
        services.remove_line(mm, only_line(order).pk)
        assert not order.lines.exists() and not PurchaseAllocation.objects.exists()
        assert flows(line).reserved == 0
        event = AuditEvent.objects.get(action="purchase_order_line.removed")
        assert event.data["allocations"] == [{"request_line_id": str(line.pk), "qty": "4"}]
        issued = buy((line, "4"))
        assert codes(services.remove_line, mm, only_line(issued).pk) == {"amend"}
        assert codes(services.update_line, mm, only_line(issued).pk, ordered_qty=D(5)) == {"amend"}


# --- issue ----------------------------------------------------------------------------


class TestIssue:
    def test_number_snapshots_and_audit(
        self, org: Org, people: People, mm: SessionPrincipal, buy: Buy, make_line: MakeLine
    ) -> None:
        first = buy((make_line("10"), "10"))
        second = buy((make_line("10"), "10"), ship_to=org.worksite(org.hospital))
        year = first.issued_at.year  # type: ignore[union-attr]
        assert (first.number, second.number) == (f"PED-A-{year}-00001", f"PED-A-{year}-00002")
        assert (first.issued_by_id, first.revision) == (people.mm_mad.pk, 0)
        assert first.ship_to_snapshot["project"] is None
        assert second.ship_to_snapshot["project"] == {"code": "HOSP-X", "name": "HOSP-X"}
        assert first.supplier_snapshot["name"] == org.supplier.name
        assert "purchase_order.issued" in actions()

    def test_idempotent_with_a_key_a_transition_error_without(
        self, people: People, mm: SessionPrincipal, buy: Buy, make_line: MakeLine
    ) -> None:
        order = buy((make_line("10"), "10"), issue=False)
        key = issue_key(people.mm_mad.pk, "purchase_order.issue")
        assert services.issue(mm, order.pk, idempotency_key=key).status == Status.ISSUED
        assert services.issue(mm, order.pk, idempotency_key=key).number == reload(order).number
        assert codes(services.issue, mm, order.pk) == {"transition"}

    def test_rules(
        self, org: Org, people: People, mm: SessionPrincipal, buy: Buy, make_line: MakeLine
    ) -> None:
        assert codes(services.issue, mm, buy(issue=False).pk) == {"empty"}
        order = buy((make_line("10"), "10"), issue=False)
        Supplier.objects.filter(pk=org.supplier.pk).update(is_active=False)
        assert codes(services.issue, mm, order.pk) == {"inactive"}
        Supplier.objects.filter(pk=org.supplier.pk).update(is_active=True)
        Location.objects.filter(pk=order.ship_to_id).update(is_active=False)
        assert codes(services.issue, mm, order.pk) == {"ship_to"}
        Location.objects.filter(pk=order.ship_to_id).update(is_active=True)
        site = buy((make_line("10"), "10"), issue=False, ship_to=org.worksite(org.hospital))
        stranger = make_line("10", project=org.sev_for_mad)
        PurchaseAllocation.objects.create(po_line=only_line(site), request_line=stranger, qty=1)
        PurchaseOrderLine.objects.filter(po=site).update(ordered_qty=D(11))
        assert codes(services.issue, mm, site.pk) == {"worksite_projects"}  # C6, edited
        Branch.objects.filter(pk=org.a_mad.pk).update(is_active=False)
        admin = principal_for(people.group_admin)  # A-MAD's manager lost the branch (S9)
        assert codes(services.issue, admin, order.pk) == {"inactive_branch"}


# --- amend ----------------------------------------------------------------------------


class TestAmend:
    def test_one_revision_with_everything_allowed(
        self,
        mm: SessionPrincipal,
        buy: Buy,
        make_line: MakeLine,
        breaker: Material,
    ) -> None:
        x, y, z = make_line("10"), make_line("10"), make_line("5", material=breaker)
        order = buy((x, "6"), (y, "4"))
        line = only_line(order)
        services.amend(
            mm,
            order.pk,
            reason="Supplier call",
            header=HeaderAmendment(
                expected_date=date(2026, 12, 1), supplier_ref=" OF-9 ", notes=""
            ),
            allocations=[
                AllocationAmendment(allocation(order, y).pk, D(1)),
                AllocationAmendment(allocation(order, x).pk, D(9)),
            ],
            lines=[LineAmendment(line.pk, ordered_qty=D(12), unit_price=D("3.5"))],
            add=[RequestItem(z.pk)],
        )
        after = reload(order)
        assert (after.revision, after.expected_date, after.supplier_ref) == (
            1,
            date(2026, 12, 1),
            "OF-9",
        )
        assert [allocation(order, item).qty for item in (x, y)] == [9, 1]
        breakers = order.lines.get(material=breaker)
        assert breakers.material_snapshot == {"reference": "DIF-40", "name": "Diferencial 40A"}
        assert flows(z).pending == 5
        event = AuditEvent.objects.get(action="purchase_order.amended")
        assert event.data["reason"] == "Supplier call" and event.data["revision"] == 1
        assert set(event.data["changes"]) == {"header", "trimmed", "lines", "raised", "added"}

    def test_an_allocation_is_not_raised_on_a_line_with_nothing_left(
        self,
        mm: SessionPrincipal,
        buy: Buy,
        make_line: MakeLine,
        received: FakeReceipts,
    ) -> None:
        x, y = make_line("10"), make_line("10")
        order = buy((x, "10"), (y, "5"))
        services.amend(
            mm,
            order.pk,
            reason="r",
            allocations=[AllocationAmendment(allocation(order, y).pk, D(4))],
        )
        received.receive(only_line(order), "15", {allocation(order, x): "10"})
        assert codes(
            services.amend,
            mm,
            order.pk,
            reason="r",
            allocations=[AllocationAmendment(allocation(order, y).pk, D(5))],
        ) == {"no_capacity"}

    def test_a_change_of_nothing_is_a_no_op(
        self, mm: SessionPrincipal, buy: Buy, make_line: MakeLine
    ) -> None:
        line = make_line("10")
        order = buy((line, "10"))
        services.amend(
            mm,
            order.pk,
            reason="Same",
            header=HeaderAmendment(notes=""),
            allocations=[AllocationAmendment(allocation(order, line).pk, D(10))],
            lines=[LineAmendment(only_line(order).pk)],
        )
        assert reload(order).revision == 0
        assert "purchase_order.amended" not in actions()

    def test_lowering_to_what_is_received_trims_and_closes(
        self,
        mm: SessionPrincipal,
        buy: Buy,
        make_line: MakeLine,
        received: FakeReceipts,
    ) -> None:
        x, y = make_line("10"), make_line("10")
        order = buy((x, "10"), (y, "10"))
        line = only_line(order)
        received.receive(line, "12", {allocation(order, x): "10", allocation(order, y): "2"})
        assert codes(
            services.amend, mm, order.pk, reason="r", lines=[LineAmendment(line.pk, D(11))]
        ) == {"below_received"}
        assert codes(
            services.amend,
            mm,
            order.pk,
            reason="r",
            lines=[LineAmendment(line.pk, unit_price=D(1))],
        ) == {"price_after_receipt"}
        services.amend(mm, order.pk, reason="Rest cancelled", lines=[LineAmendment(line.pk, D(12))])
        assert [allocation(order, item).qty for item in (x, y)] == [10, 2]
        assert reload(order).status == Status.CLOSED
        assert "purchase_order.auto_closed" in actions()

    def test_rules(
        self, people: People, mm: SessionPrincipal, buy: Buy, make_line: MakeLine
    ) -> None:
        line = make_line("10")
        order = buy((line, "10"))
        assert codes(services.amend, mm, order.pk, reason=" ") == {"required"}
        with pytest.raises(ValueError, match="once per call"):
            services.amend(
                mm,
                order.pk,
                reason="r",
                lines=[LineAmendment(only_line(order).pk), LineAmendment(only_line(order).pk)],
            )
        other = buy((make_line("10"), "10"))
        assert codes(
            services.amend, mm, order.pk, reason="r", lines=[LineAmendment(only_line(other).pk)]
        ) == {"unknown"}
        foreign = PurchaseAllocation.objects.get(po_line__po=other)
        assert codes(
            services.amend,
            mm,
            order.pk,
            reason="r",
            allocations=[AllocationAmendment(foreign.pk, D(1))],
        ) == {"unknown"}
        assert codes(
            services.amend,
            mm,
            order.pk,
            reason="r",
            allocations=[AllocationAmendment(allocation(order, line).pk, D("0.001"))],
        ) == {"precision"}
        assert codes(
            services.amend,
            mm,
            order.pk,
            reason="r",
            allocations=[AllocationAmendment(allocation(order, line).pk, D(11))],
        ) == {"exceeds_ordered"}
        services.close_line_short(mm, only_line(order).pk, reason="Gone")
        draft = buy(issue=False)
        assert codes(services.amend, mm, draft.pk, reason="r") == {"transition"}
        reopened = buy((make_line("10"), "10"))
        closed_line = only_line(reopened)
        PurchaseOrderLine.objects.filter(pk=closed_line.pk).update(
            closed_short_at=closed_line.created_at, close_reason="x"
        )
        assert codes(
            services.amend,
            mm,
            reopened.pk,
            reason="r",
            lines=[LineAmendment(closed_line.pk, D(20))],
        ) == {"closed_short"}
        key = issue_key(people.mm_mad.pk, "purchase_order.amend")
        fresh = buy((make_line("10"), "5"))
        for _ in range(2):  # a double tap with the key: one revision
            services.amend(
                mm, fresh.pk, reason="r", header=HeaderAmendment(notes="Twice"), idempotency_key=key
            )
        assert reload(fresh).revision == 1


# --- cancel and close -----------------------------------------------------------------


class TestCancel:
    def test_a_draft_is_discarded(
        self, mm: SessionPrincipal, buy: Buy, make_line: MakeLine
    ) -> None:
        line = make_line("10")
        draft = buy((line, "10"), issue=False)
        assert services.cancel(mm, draft.pk).status == Status.CANCELLED
        assert allocation(draft, line).qty == 0
        assert flows(line).reserved == 0
        assert "purchase_order.discarded" in actions()
        assert codes(services.cancel, mm, draft.pk) == {"transition"}

    def test_an_issued_order_with_its_dispatches(
        self, mm: SessionPrincipal, buy: Buy, make_line: MakeLine
    ) -> None:
        line = make_line("10")
        order = buy((line, "10"))
        assert codes(services.cancel, mm, order.pk) == {"required"}
        queued, sent = (
            EmailOutbox.objects.create(
                kind=Kind.PO_ORDER,
                status=status,
                recipient_address="orders@supplier.example",
                source_po=order,
                from_email="app@example.org",
                subject="Order",
                body="b",
            )
            for status in (MailStatus.QUEUED, MailStatus.SENT)
        )
        cancelled = services.cancel(mm, order.pk, reason="Project stopped")
        assert (cancelled.status, cancelled.cancellation_notice_required) == (
            Status.CANCELLED,
            True,
        )
        assert EmailOutbox.objects.get(pk=queued.pk).status == MailStatus.CANCELLED
        assert EmailOutbox.objects.get(pk=sent.pk).status == MailStatus.SENT
        assert allocation(order, line).qty == 0 and flows(line).pending == 0
        event = AuditEvent.objects.get(action="purchase_order.cancelled")
        assert event.data["dispatches_cancelled"] == 1

    def test_not_after_a_receipt_nor_once_closed(
        self,
        mm: SessionPrincipal,
        buy: Buy,
        make_line: MakeLine,
        received: FakeReceipts,
    ) -> None:
        line = make_line("10")
        order = buy((line, "10"))
        received.receive(only_line(order), "1", {allocation(order, line): "1"})
        assert codes(services.cancel, mm, order.pk, reason="r") == {"has_receipts"}
        services.close_line_short(mm, only_line(order).pk, reason="Short")
        assert reload(order).status == Status.CLOSED
        assert codes(services.cancel, mm, order.pk, reason="r") == {"transition"}

    def test_a_status_change_before_the_lock_retries(
        self,
        mm: SessionPrincipal,
        buy: Buy,
        make_line: MakeLine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        draft = buy((make_line("10"), "10"), issue=False)
        real = common._lock_order

        def issued_meanwhile(locks: LockOrder, order: PurchaseOrder, **seeds: Any) -> Any:
            PurchaseOrder.objects.filter(pk=order.pk).update(
                status=Status.ISSUED,
                number="PED-X",
                issued_at=order.created_at,
                issued_by_id=mm.user_id,
            )
            return real(locks, order, **seeds)

        monkeypatch.setattr(orders, "_lock_order", issued_meanwhile)
        with pytest.raises(LockSetChanged):
            services.cancel(mm, draft.pk)


class TestCloseLineShort:
    def test_rules_and_an_order_that_stays_open(
        self, mm: SessionPrincipal, buy: Buy, make_line: MakeLine, breaker: Material
    ) -> None:
        order = buy((make_line("10"), "10"), (make_line("3", material=breaker), "3"))
        cable_line, breaker_line = order.lines.order_by("line_no")
        assert codes(services.close_line_short, mm, cable_line.pk, reason=" ") == {"required"}
        services.close_line_short(mm, cable_line.pk, reason="Discontinued")
        assert reload(order).status == Status.ISSUED  # the breakers can still arrive
        assert codes(services.close_line_short, mm, cable_line.pk, reason="x") == {"closed_short"}
        draft = buy((make_line("10"), "10"), issue=False)
        assert codes(services.close_line_short, mm, only_line(draft).pk, reason="x") == {
            "transition"
        }
        assert breaker_line.closed_short_at is None

    def test_a_line_received_in_full_is_not_closed_short(
        self,
        mm: SessionPrincipal,
        buy: Buy,
        make_line: MakeLine,
        breaker: Material,
        received: FakeReceipts,
    ) -> None:
        line = make_line("10")
        order = buy((line, "10"), (make_line("3", material=breaker), "3"))
        cable_line = order.lines.get(line_no=1)
        received.receive(cable_line, "10", {allocation(order, line): "10"})
        assert codes(services.close_line_short, mm, cable_line.pk, reason="x") == {"received"}
        assert cable_line.closed_short_at is None


class TestSettle:
    def test_a_closed_order_with_capacity_again_reopens(
        self,
        mm: SessionPrincipal,
        buy: Buy,
        make_line: MakeLine,
        received: FakeReceipts,
    ) -> None:
        """What a receipt void does (M6.1): the order goes back to ``ISSUED``."""
        line = make_line("10")
        order = buy((line, "10"))
        received.receive(only_line(order), "10", {allocation(order, line): "10"})
        with transaction.atomic():
            locked, component = common._lock_order(LockOrder(), order)
            common._settle_order(mm, locked, component)
            assert locked.status == Status.CLOSED
            received.received.clear()
            common._settle_order(mm, locked, component)
        assert reload(order).status == Status.ISSUED
        assert actions()[-2:] == ["purchase_order.auto_closed", "purchase_order.auto_reopened"]
        assert isinstance(component, Component)


def test_the_package_exports_the_services() -> None:
    assert set(services.__all__) >= {"create_po", "issue", "amend", "cancel", "allocate"}
