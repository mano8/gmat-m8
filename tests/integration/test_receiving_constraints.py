"""Database rules of receipts and handovers (plan §4.6 C1-C3, I8, I10, I11), whatever writes.

Rows are written with the ORM directly, past the services, so each test shows the database
itself refusing the row: the composite foreign keys, the unique attribution of a direct
handover line, the CHECK constraints and the triggers that freeze posted documents.
"""

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.catalog.models import Material, UnitOfMeasure
from apps.handovers.models import Handover, HandoverLine
from apps.material_requests.models import MaterialRequest, MaterialRequestLine
from apps.projects.models import Project
from apps.purchasing.models import PurchaseAllocation, PurchaseOrder, PurchaseOrderLine
from apps.receiving.models import GoodsReceipt, GoodsReceiptLine, ReceiptAttribution
from tests import orders
from tests.org import Org, People

pytestmark = [pytest.mark.integration, pytest.mark.django_db]

D = Decimal


def refused(match: str = "") -> Any:
    return pytest.raises(IntegrityError, match=match or None)


@dataclass
class World:
    order: PurchaseOrder
    line: PurchaseOrderLine
    allocation: PurchaseAllocation
    other_order: PurchaseOrder
    other_line: PurchaseOrderLine
    other_allocation: PurchaseAllocation
    receipt: GoodsReceipt
    receipt_line: GoodsReceiptLine
    attribution: ReceiptAttribution
    handover: Handover
    handover_line: HandoverLine
    sev_line: MaterialRequestLine  # a request line of another project (SEV-2)


def approved_line(people: People, material: Material, project: Project) -> MaterialRequestLine:
    request = MaterialRequest.objects.create(
        project=project,
        procurement_branch_id=project.procurement_branch_id,
        requested_by=people.tech_mad,
        status=MaterialRequest.Status.APPROVED,
        number=f"SOL-I-{project.code}",
        submitted_at=timezone.now(),
    )
    return MaterialRequestLine.objects.create(
        request=request,
        project=project,
        line_no=1,
        material=material,
        unit_id=material.unit_id,
        requested_qty=D(10),
        approved_qty=D(10),
        decision="APPROVED",
        decided_by=people.mm_mad,
        decided_at=timezone.now(),
        sourcing="PURCHASE",
    )


@pytest.fixture
def world(org: Org, people: People) -> World:
    material = Material.objects.create(
        reference="CAB-I", name="Cable", unit=UnitOfMeasure.objects.get(code="M")
    )
    hospital = approved_line(people, material, org.hospital)
    sev_line = approved_line(people, material, org.sev_for_mad)
    order = orders.order(org, status="ISSUED", issued_by=people.mm_mad)
    line = orders.line(order, material, "10")
    allocation = orders.allocate(line, hospital, "10")
    other_order = orders.order(org, status="ISSUED", issued_by=people.mm_mad)
    other_line = orders.line(other_order, material, "10")
    other_allocation = orders.allocate(other_line, sev_line, "10")
    now = timezone.now()
    receipt = GoodsReceipt.objects.create(
        number="REC-I-00001",
        po=order,
        branch=org.a_mad,
        location=org.worksite(org.hospital),
        received_at=now,
        received_by=people.mm_mad,
    )
    receipt_line = GoodsReceiptLine.objects.create(
        receipt=receipt, po=order, po_line=line, qty_received=D(4)
    )
    attribution = ReceiptAttribution.objects.create(
        receipt_line=receipt_line, allocation=allocation, po_line=line, qty=D(4)
    )
    handover = Handover.objects.create(
        number="ENT-I-00001",
        kind=Handover.Kind.DIRECT_RECEIPT,
        branch=org.a_mad,
        source_location=receipt.location,
        project=org.hospital,
        handed_by=people.mm_mad,
        received_by=people.mm_mad,
        handed_at=now,
        goods_receipt=receipt,
    )
    handover_line = HandoverLine.objects.create(
        handover=handover,
        project=org.hospital,
        request_line=hospital,
        qty=D(4),
        receipt_attribution=attribution,
    )
    return World(
        order,
        line,
        allocation,
        other_order,
        other_line,
        other_allocation,
        receipt,
        receipt_line,
        attribution,
        handover,
        handover_line,
        sev_line,
    )


def atomic(write: Callable[[], Any], match: str = "") -> None:
    with refused(match), transaction.atomic():
        write()


class TestC1:
    def test_a_receipt_line_on_another_order_than_its_receipt(self, world: World) -> None:
        atomic(
            lambda: GoodsReceiptLine.objects.create(
                receipt=world.receipt,
                po=world.other_order,
                po_line=world.other_line,
                qty_received=D(1),
            ),
            "rcpt_line_receipt_order_fk",
        )

    def test_a_receipt_line_whose_order_line_is_of_another_order(self, world: World) -> None:
        atomic(
            lambda: GoodsReceiptLine.objects.create(
                receipt=world.receipt, po=world.order, po_line=world.other_line, qty_received=D(1)
            ),
            "rcpt_line_po_line_order_fk",
        )


class TestC2:
    def test_an_allocation_of_another_order_line(self, world: World) -> None:
        atomic(
            lambda: ReceiptAttribution.objects.create(
                receipt_line=world.receipt_line,
                allocation=world.other_allocation,
                po_line=world.line,
                qty=D(1),
            ),
            "rcpt_attribution_allocation_line_fk",
        )

    def test_an_order_line_that_is_not_the_received_one(self, world: World) -> None:
        atomic(
            lambda: ReceiptAttribution.objects.create(
                receipt_line=world.receipt_line,
                allocation=world.other_allocation,
                po_line=world.other_line,
                qty=D(1),
            ),
            "rcpt_attribution_receipt_line_fk",
        )


class TestC3:
    def test_a_request_line_of_another_project(self, world: World) -> None:
        atomic(
            lambda: HandoverLine.objects.create(
                handover=world.handover,
                project=world.handover.project,
                request_line=world.sev_line,
                qty=D(1),
            ),
            "ho_line_request_line_project_fk",
        )

    def test_a_line_on_another_project_than_its_handover(self, world: World) -> None:
        atomic(
            lambda: HandoverLine.objects.create(
                handover=world.handover,
                project=world.sev_line.project,
                request_line=world.sev_line,
                qty=D(1),
            ),
            "ho_line_handover_project_fk",
        )


class TestI8:
    def test_an_attribution_is_handed_over_once(self, world: World) -> None:
        atomic(
            lambda: HandoverLine.objects.create(
                handover=world.handover,
                project=world.handover.project,
                request_line=world.handover_line.request_line,
                qty=D(4),
                receipt_attribution=world.attribution,
            ),
            "receipt_attribution",
        )

    def test_a_receipt_has_one_direct_handover(self, world: World, people: People) -> None:
        atomic(
            lambda: Handover.objects.create(
                number="ENT-I-00002",
                kind=Handover.Kind.DIRECT_RECEIPT,
                branch=world.handover.branch,
                source_location=world.handover.source_location,
                project=world.handover.project,
                handed_by=people.mm_mad,
                received_by=people.mm_mad,
                handed_at=timezone.now(),
                goods_receipt=world.receipt,
            ),
            "goods_receipt",
        )


class TestChecks:
    @pytest.mark.parametrize(
        ("values", "name"),
        [
            ({"qty_received": D(0), "qty_rejected": D(0)}, "rcpt_line_not_empty"),
            ({"qty_received": D(1), "surplus_qty": D(2)}, "rcpt_line_surplus_within_received"),
            ({"qty_received": D(-1), "qty_rejected": D(2)}, "rcpt_line_quantities_not_negative"),
            ({"qty_received": D(1), "issue": "LOST"}, "rcpt_line_issue_valid"),
        ],
    )
    def test_receipt_lines(self, world: World, values: dict[str, Any], name: str) -> None:
        receipt = GoodsReceipt.objects.create(
            number="REC-I-00002",
            po=world.order,
            branch=world.receipt.branch,
            location=world.receipt.location,
            received_at=timezone.now(),
            received_by=world.receipt.received_by,
        )
        atomic(
            lambda: GoodsReceiptLine.objects.create(
                receipt=receipt, po=world.order, po_line=world.line, **values
            ),
            name,
        )

    def test_an_attribution_is_positive(self, world: World) -> None:
        line = GoodsReceiptLine.objects.create(
            receipt=GoodsReceipt.objects.create(
                number="REC-I-00003",
                po=world.order,
                branch=world.receipt.branch,
                location=world.receipt.location,
                received_at=timezone.now(),
                received_by=world.receipt.received_by,
            ),
            po=world.order,
            po_line=world.line,
            qty_received=D(1),
        )
        atomic(
            lambda: ReceiptAttribution.objects.create(
                receipt_line=line, allocation=world.allocation, po_line=world.line, qty=D(0)
            ),
            "rcpt_attribution_qty_positive",
        )

    def test_a_voided_receipt_has_its_void_fields(self, world: World) -> None:
        atomic(
            lambda: GoodsReceipt.objects.filter(pk=world.receipt.pk).update(status="VOIDED"),
            "rcpt_void_fields",
        )

    @pytest.mark.parametrize(
        ("values", "name"),
        [
            ({"kind": "FROM_WAREHOUSE"}, "ho_direct_has_receipt"),
            ({"received_by": None, "received_by_name": ""}, "ho_receiver_named"),
            ({"kind": "BY_POST"}, "ho_direct_has_receipt|ho_kind_valid"),
        ],
    )
    def test_handovers(self, world: World, values: dict[str, Any], name: str) -> None:
        def create() -> None:
            fields: dict[str, Any] = {
                "number": "ENT-I-00009",
                "kind": Handover.Kind.DIRECT_RECEIPT,
                "branch": world.handover.branch,
                "source_location": world.handover.source_location,
                "project": world.handover.project,
                "handed_by": world.handover.handed_by,
                "received_by": world.handover.handed_by,
                "handed_at": timezone.now(),
                "goods_receipt": GoodsReceipt.objects.create(
                    number="REC-I-00009",
                    po=world.order,
                    branch=world.receipt.branch,
                    location=world.receipt.location,
                    received_at=timezone.now(),
                    received_by=world.receipt.received_by,
                ),
            }
            Handover.objects.create(**(fields | values))

        atomic(create, name)

    def test_a_handover_line_is_positive(self, world: World) -> None:
        atomic(
            lambda: HandoverLine.objects.create(
                handover=world.handover,
                project=world.handover.project,
                request_line=world.handover_line.request_line,
                qty=D(0),
            ),
            "ho_line_qty_positive",
        )


class TestPostedDocumentsAreFrozen:
    """I11: only the void fields of a posted receipt or handover change, once."""

    def test_a_posted_receipt_changes_only_by_its_void(self, world: World) -> None:
        atomic(
            lambda: GoodsReceipt.objects.filter(pk=world.receipt.pk).update(notes="edited"),
            "changes only by its void",
        )
        GoodsReceipt.objects.filter(pk=world.receipt.pk).update(
            status="VOIDED",
            voided_at=timezone.now(),
            voided_by=world.receipt.received_by,
            void_reason="Wrong order",
        )
        atomic(
            lambda: GoodsReceipt.objects.filter(pk=world.receipt.pk).update(void_reason="other"),
            "changes only by its void",
        )

    def test_receipts_are_never_deleted(self, world: World) -> None:
        receipt = GoodsReceipt.objects.create(
            number="REC-I-00004",
            po=world.order,
            branch=world.receipt.branch,
            location=world.receipt.location,
            received_at=timezone.now(),
            received_by=world.receipt.received_by,
        )
        atomic(receipt.delete, "never deleted")
        rows = GoodsReceipt.objects.filter(pk=world.receipt.pk)
        atomic(lambda: rows._raw_delete(rows.db), "never deleted")

    @pytest.mark.parametrize("model", ["receipt_line", "attribution", "handover_line"])
    def test_lines_never_change(self, world: World, model: str) -> None:
        row = getattr(world, model)
        rows = type(row).objects.filter(pk=row.pk)
        field = "qty_received" if model == "receipt_line" else "qty"
        atomic(lambda: rows.update(**{field: D(5)}), "never changed")
        # Straight to the database: Django's own PROTECT check would refuse it first.
        atomic(lambda: rows._raw_delete(rows.db), "never changed")

    def test_a_posted_handover_changes_only_by_its_void(self, world: World) -> None:
        atomic(
            lambda: Handover.objects.filter(pk=world.handover.pk).update(received_by_name="x"),
            "changes only by its void",
        )
        rows = Handover.objects.filter(pk=world.handover.pk)
        atomic(lambda: rows._raw_delete(rows.db), "never deleted")
        Handover.objects.filter(pk=world.handover.pk).update(
            status="VOIDED",
            voided_at=timezone.now(),
            voided_by=world.handover.handed_by,
            void_reason="With its receipt",
        )
        atomic(
            lambda: Handover.objects.filter(pk=world.handover.pk).update(status="POSTED"),
            "changes only by its void",
        )
