"""What the history of reviewer edits records, read by the services and the selectors
(M7b.3, ADR-0014 §5-§6).

- ``values``: a line's values as a revision stores them (``before`` / ``after``): what the
  line asks for and how it was decided. Quantities are normalized strings, ids strings,
  so the JSON reads the same whatever the database returns; no prices, suppliers or
  orders, which a requester could not see.
- ``version``: how many times the request was submitted plus how many edits it had. Both
  only grow (append-only rows, written under the request's header lock), so a page that
  sends back the version it showed lets a decision or an edit be refused once someone
  else edited the request (or it was resubmitted) in between.
"""

from decimal import Decimal
from typing import Any
from uuid import UUID

from apps.material_requests.models import (
    MaterialRequestBaseline,
    MaterialRequestLine,
    MaterialRequestLineRevision,
)


def quantity(value: Decimal | None) -> str | None:
    return None if value is None else f"{value.normalize():f}"


def values(line: MaterialRequestLine) -> dict[str, Any]:
    """The line's material, description, unit, quantities and decision (no other data)."""
    material = line.material
    return {
        "line_no": line.line_no,
        "material_id": None if material is None else str(material.pk),
        "reference": "" if material is None else material.reference,
        "name": "" if material is None else material.name,
        "free_text": line.free_text,
        "unit_id": str(line.unit_id),
        "unit": line.unit.code,
        "requested_qty": quantity(line.requested_qty),
        "approved_qty": quantity(line.approved_qty),
        "decision": line.decision,
        "closed_short": line.closed_short_at is not None,
    }


def version(request_id: UUID) -> int:
    """Submits plus edits of the request (see the module note)."""
    return (
        MaterialRequestBaseline.objects.filter(request_id=request_id).count()
        + MaterialRequestLineRevision.objects.filter(request_id=request_id).count()
    )
