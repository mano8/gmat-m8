"""What posted receipts contribute to purchasing (plan §4.5): the ``ReceiptSource``.

- ``lines``: per order line, Σ ``qty_received`` on ``POSTED`` receipts (received), and
  whether a posted receipt line exists at all, even one that rejected everything (such an
  order is no longer cancelled and such a line keeps its price, §4.4);
- ``attributed``: per allocation, Σ its attributions on ``POSTED`` receipts.

A voided receipt counts for nothing. Readers call these under the locks of the component
they compute (L2-L5), which posting and voiding take first (§4.7).
"""

from collections.abc import Collection, Mapping
from decimal import Decimal
from uuid import UUID

from django.db.models import Sum

from apps.purchasing.receipts import LineReceipts
from apps.receiving.models import GoodsReceipt, GoodsReceiptLine, ReceiptAttribution

POSTED = GoodsReceipt.Status.POSTED


class PostedReceipts:
    """Purchasing's ``ReceiptSource``, registered from ``ReceivingConfig.ready``."""

    def lines(self, po_line_ids: Collection[UUID]) -> Mapping[UUID, LineReceipts]:
        rows = (
            GoodsReceiptLine.objects.filter(
                po_line_id__in=list(po_line_ids), receipt__status=POSTED
            )
            .values("po_line_id")
            .annotate(received=Sum("qty_received"))
            .values_list("po_line_id", "received")
        )
        return {line_id: LineReceipts(received, posted=True) for line_id, received in rows}

    def attributed(self, allocation_ids: Collection[UUID]) -> Mapping[UUID, Decimal]:
        rows = (
            ReceiptAttribution.objects.filter(
                allocation_id__in=list(allocation_ids),
                receipt_line__receipt__status=POSTED,
            )
            .values("allocation_id")
            .annotate(total=Sum("qty"))
            .values_list("allocation_id", "total")
        )
        return dict(rows)


SOURCE = PostedReceipts()
