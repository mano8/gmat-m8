"""What goods receipts contribute to purchase-order lines and allocations (plan §4.5).

Receiving (M6.1) refers to order lines and allocations, so this app cannot import it: it
registers a ``ReceiptSource`` from its ``AppConfig.ready``. Until then nothing is received.

- ``lines``: per order line, Σ ``qty_received`` on ``POSTED`` receipts (received; the
  line's remaining is ordered - received) and whether any posted receipt line exists (an
  order with one cannot be cancelled; a line with one keeps its price, §4.4);
- ``attributed``: per allocation, Σ its attributions on ``POSTED`` receipts. R of a request
  line is the sum over its allocations on every order (F69).

Readers call these under the locks of the component they compute (L2-L5): posting and
voiding a receipt lock that component first (§4.7), so the figures cannot move under them.
"""

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol
from uuid import UUID

ZERO = Decimal("0")


@dataclass(frozen=True)
class LineReceipts:
    received: Decimal = ZERO
    posted: bool = False

    def __add__(self, other: LineReceipts) -> LineReceipts:
        return LineReceipts(self.received + other.received, self.posted or other.posted)


class ReceiptSource(Protocol):
    def lines(self, po_line_ids: Collection[UUID]) -> Mapping[UUID, LineReceipts]: ...

    def attributed(self, allocation_ids: Collection[UUID]) -> Mapping[UUID, Decimal]: ...


_sources: list[ReceiptSource] = []


def register_receipt_source(source: ReceiptSource) -> Callable[[], None]:
    """Register ``source`` (from an ``AppConfig.ready``); return its unregister function."""
    if source not in _sources:
        _sources.append(source)

    def unregister() -> None:
        if source in _sources:
            _sources.remove(source)

    return unregister


def line_receipts(po_line_ids: Collection[UUID]) -> dict[UUID, LineReceipts]:
    """Received and posted per order line, summed over every registered source."""
    result = {line_id: LineReceipts() for line_id in po_line_ids}
    if result:
        for source in _sources:
            for line_id, part in source.lines(list(result)).items():
                result[line_id] = result[line_id] + part
    return result


def attributed(allocation_ids: Collection[UUID]) -> dict[UUID, Decimal]:
    """Attributed per allocation, summed over every registered source."""
    result = dict.fromkeys(allocation_ids, ZERO)
    if result:
        for source in _sources:
            for allocation_id, quantity in source.attributed(list(result)).items():
                result[allocation_id] += quantity
    return result
