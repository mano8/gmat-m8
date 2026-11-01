"""The purchasing screens (plan §11 M5.2): worklist, builder, order lists, order page,
amendment and printable page; M5.3 adds the supplier dispatches on the order page.

Reads go through ``apps.purchasing.selectors``; writes through ``apps.purchasing.services``
from POST-only views (``@mutating``), each on its own URL, with its GET page on another.
Capabilities: ``po.manage`` (group administrators, material managers) for the worklist,
the builder and every action; ``po.view`` (also warehouse operators) for the order lists,
the order page and the printable page. Prices appear only with ``price.view`` (§8.2).
"""

from apps.purchasing.views.actions import (
    allocation_increase,
    allocation_unallocate,
    dispatch_confirm,
    dispatch_resend,
    line_close_short,
    line_remove,
    line_update,
    lines_add,
    order_amend,
    order_cancel,
    order_create,
    order_issue,
    order_update,
)
from apps.purchasing.views.pages import (
    order_amendment,
    order_detail,
    order_list,
    order_new,
    order_print,
    worklist,
)

__all__ = [
    "allocation_increase",
    "allocation_unallocate",
    "dispatch_confirm",
    "dispatch_resend",
    "line_close_short",
    "line_remove",
    "line_update",
    "lines_add",
    "order_amend",
    "order_amendment",
    "order_cancel",
    "order_create",
    "order_detail",
    "order_issue",
    "order_list",
    "order_new",
    "order_print",
    "order_update",
    "worklist",
]
