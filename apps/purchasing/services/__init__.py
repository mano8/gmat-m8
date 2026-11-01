"""Purchase orders: drafts, allocations, issue, amendments, cancellation (plan §4.3 to §4.9).

Each service runs in one transaction with ``lock_timeout`` through the shared retry
wrapper, in the order of CLAUDE.md "Writes":

1. ``authorize_in_txn`` (L0, in full, every time): ``po.manage`` on the order (on the
   branch and destination of a new one) and on the requests it allocates to, with
   ``po.view`` as the read capability (404 outside it, 403 without ``po.manage``); setting
   a price also needs ``price.view``;
2. the idempotency record of a create, an issue or an amendment (L1);
3. the allocation component (``components.Component.lock``): every request header (L2) and
   line (L3), then every order header (L4; an order's dispatch rows at L4b) and line (L5)
   connected to the order through allocations, discovered, locked and verified; a
   component of more than 200 lines is refused;
4. the change, validated under those locks: the editing rules of §4.4, C5 for every
   allocation create or increase, C6 for worksite destinations, C10 and S9;
5. ``reconcile`` (§4.5): excess commitments in the component are cut and audited;
6. the document number last (L9), so a rollback leaves no gap.

Supplier dispatches (M5.3, ``dispatches``): a human confirms or resends a dispatch the
worker left ``AMBIGUOUS`` or ``FAILED``; the order at L4, then its dispatch rows at L4b.

Capabilities (plan §3.4): ``po.manage`` for every write (group administrators and material
managers); ``price.view`` for prices. Prices are optional (Q10). A draft changes freely and
each allocation reserves demand; an issued order changes only through ``amend``.
"""

from apps.purchasing.services.common import RequestItem
from apps.purchasing.services.dispatches import confirm_dispatch, resend_dispatch
from apps.purchasing.services.drafts import (
    add_request_lines,
    allocate,
    create_po,
    increase_allocation,
    remove_line,
    unallocate,
    update_line,
    update_order,
)
from apps.purchasing.services.orders import (
    AllocationAmendment,
    LineAmendment,
    amend,
    cancel,
    close_line_short,
    issue,
)

__all__ = [
    "AllocationAmendment",
    "LineAmendment",
    "RequestItem",
    "add_request_lines",
    "allocate",
    "amend",
    "cancel",
    "close_line_short",
    "confirm_dispatch",
    "create_po",
    "increase_allocation",
    "issue",
    "remove_line",
    "resend_dispatch",
    "unallocate",
    "update_line",
    "update_order",
]
