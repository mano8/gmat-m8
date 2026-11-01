"""Reads of handovers (plan §2.3 point 3, §4.5 I9).

Warehouse stock is **not tracked** in phase 1: material may have been there before the
application. What a warehouse received and has not handed over yet is therefore only a
hint, shown as a warning on the handover form and never a limit:

    available = Σ accepted on posted receipts at that warehouse
              - Σ handed over from it on posted ``FROM_WAREHOUSE`` handovers

per material, never below zero. Only receipts at that warehouse count (I9); goods received
on a worksite never entered a warehouse. A line without a catalog material has no figure.
The figures are for whoever may post handovers from that warehouse (``handover.post``).
"""

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from django.db.models import Sum

from apps.access.context import AccessContext
from apps.access.exceptions import NotInScope
from apps.access.roles import Capability
from apps.catalog.models import Material
from apps.handovers.models import Handover, HandoverLine
from apps.locations.models import Location
from apps.material_requests.models import MaterialRequestLine
from apps.receiving.models import GoodsReceipt, GoodsReceiptLine

POST = Capability.HANDOVER_POST
ZERO = Decimal("0")


@dataclass(frozen=True)
class Shortfall:
    """More of a material is being handed over than the warehouse is known to hold."""

    material_id: UUID
    reference: str
    name: str
    unit: str
    wanted: Decimal
    available: Decimal


def _warehouse(ctx: AccessContext, location_id: UUID) -> Location:
    location = (
        Location.objects.visible_to(ctx, POST)
        .filter(pk=location_id, kind=Location.Kind.WAREHOUSE)
        .first()
    )
    if location is None:
        raise NotInScope(POST)
    return location


def warehouse_stock(
    ctx: AccessContext, location_id: UUID, material_ids: Collection[UUID]
) -> dict[UUID, Decimal]:
    """What this warehouse is known to hold of each material (404 outside its scope)."""
    location = _warehouse(ctx, location_id)
    ids = list(material_ids)
    received = dict(
        GoodsReceiptLine.objects.filter(
            receipt__location=location,
            receipt__status=GoodsReceipt.Status.POSTED,
            po_line__material_id__in=ids,
        )
        .values("po_line__material_id")
        .annotate(total=Sum("qty_received"))
        .values_list("po_line__material_id", "total")
    )
    handed = dict(
        HandoverLine.objects.filter(
            handover__source_location=location,
            handover__kind=Handover.Kind.FROM_WAREHOUSE,
            handover__status=Handover.Status.POSTED,
            request_line__material_id__in=ids,
        )
        .values("request_line__material_id")
        .annotate(total=Sum("qty"))
        .values_list("request_line__material_id", "total")
    )
    return {
        material: max(ZERO, received.get(material, ZERO) - handed.get(material, ZERO))
        for material in ids
    }


def stock_warnings(
    ctx: AccessContext, location_id: UUID, quantities: Mapping[UUID, Decimal]
) -> list[Shortfall]:
    """The materials of a handover (request line → quantity) beyond the warehouse's stock.

    Request lines outside ``handover.post`` are ignored, like lines without a material.
    """
    lines = list(
        MaterialRequestLine.objects.visible_to(ctx, POST)
        .filter(pk__in=list(quantities))
        .select_related("material", "unit")
    )
    wanted: dict[UUID, Decimal] = {}
    materials: dict[UUID, tuple[Material, str]] = {}
    for line in lines:
        if line.material is not None:
            materials[line.material.pk] = (line.material, line.unit.code)
            wanted[line.material.pk] = wanted.get(line.material.pk, ZERO) + quantities[line.pk]
    stock = warehouse_stock(ctx, location_id, wanted)
    shortfalls = [
        Shortfall(
            material_id=material.pk,
            reference=material.reference,
            name=material.name,
            unit=unit,
            wanted=wanted[material.pk],
            available=stock[material.pk],
        )
        for material, unit in materials.values()
        if wanted[material.pk] > stock[material.pk]
    ]
    return sorted(shortfalls, key=lambda item: item.reference)
