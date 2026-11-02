"""What posted handovers contribute to request lines (plan §4.5): delivered D.

D is Σ ``HandoverLine.qty`` on ``POSTED`` handovers of both kinds; a voided handover counts
for nothing. Registered with ``material_requests.ledger`` as a ``FlowSource`` from
``HandoversConfig.ready``. Per the consumption rule of phase 1, a line's handovers consume
its received quantity first; whatever is handed over beyond it came from untracked stock.
"""

from collections.abc import Collection
from uuid import UUID

from django.db.models import Sum

from apps.handovers.models import Handover, HandoverLine
from apps.material_requests.ledger import Flows


def delivered(line_ids: Collection[UUID]) -> dict[UUID, Flows]:
    """D of these request lines (a ``FlowSource``)."""
    if not line_ids:
        return {}
    rows = (
        HandoverLine.objects.filter(
            request_line_id__in=list(line_ids), handover__status=Handover.Status.POSTED
        )
        .values("request_line_id")
        .annotate(total=Sum("qty"))
        .values_list("request_line_id", "total")
    )
    return {line_id: Flows(delivered=total) for line_id, total in rows}
