"""The receiving screens (plan §11 M6.3): receipts, the receipt form, the expected deliveries.

Reads go through ``apps.receiving.selectors``; writes through ``apps.receiving.services``
from POST-only views (``@mutating``), each on its own URL, with its GET page on another.
Capabilities: ``receipt.view`` for the lists, the receipt page and the expected deliveries;
``receipt.post`` for finding an order and the receipt form; ``receipt.void`` (group
administrators, material managers) for voiding. Prices never appear on these screens.
"""

from apps.receiving.views.actions import receipt_post, receipt_void
from apps.receiving.views.pages import (
    expected,
    order_search,
    project_list,
    receipt_detail,
    receipt_list,
    receive,
)

__all__ = [
    "expected",
    "order_search",
    "project_list",
    "receipt_detail",
    "receipt_list",
    "receipt_post",
    "receipt_void",
    "receive",
]
