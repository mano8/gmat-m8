"""Purchase orders written directly with the ORM (fixtures only; the app uses services).

``order`` builds a draft or issued order of A-MAD to the shared fixture's supplier;
``line`` and ``allocate`` add to it. Issued orders get a number and their snapshots, as
``services.issue`` would leave them.
"""

from decimal import Decimal
from typing import Any

from django.utils import timezone

from apps.accounts.models import User
from apps.catalog.models import Material
from apps.locations.models import Location
from apps.material_requests.models import MaterialRequestLine
from apps.organizations.models import Branch
from apps.purchasing.models import PurchaseAllocation, PurchaseOrder, PurchaseOrderLine
from tests.org import Org

Status = PurchaseOrder.Status
_numbers = iter(range(1, 100_000))


def order(
    org: Org,
    *,
    status: str = Status.DRAFT,
    branch: Branch | None = None,
    ship_to: Location | None = None,
    issued_by: User | None = None,
    **fields: Any,
) -> PurchaseOrder:
    """An order of ``branch`` (A-MAD) to ``org.supplier``; issued orders need ``issued_by``."""
    branch = branch or org.a_mad
    issued = status in {Status.ISSUED, Status.CLOSED}
    values: dict[str, Any] = {
        "branch": branch,
        "buyer_company_id": branch.company_id,
        "supplier": org.supplier,
        "ship_to": ship_to or org.warehouses[branch.name],
        "status": status,
    }
    if issued:
        assert issued_by is not None, "an issued order needs its issuer"
        values |= {
            "number": f"PED-T-{next(_numbers):05d}",
            "issued_at": timezone.now(),
            "issued_by": issued_by,
            "supplier_snapshot": {"name": org.supplier.name},
        }
    return PurchaseOrder.objects.create(**(values | fields))


def line(
    po: PurchaseOrder, material: Material, ordered: Decimal | str, **fields: Any
) -> PurchaseOrderLine:
    """The next line of ``po`` for ``material``, in its unit."""
    number = po.lines.count() + 1
    return PurchaseOrderLine.objects.create(
        po=po,
        line_no=number,
        material=material,
        unit_id=material.unit_id,
        ordered_qty=Decimal(ordered),
        **fields,
    )


def allocate(
    po_line: PurchaseOrderLine, request_line: MaterialRequestLine, qty: Decimal | str
) -> PurchaseAllocation:
    return PurchaseAllocation.objects.create(
        po_line=po_line, request_line=request_line, qty=Decimal(qty)
    )
