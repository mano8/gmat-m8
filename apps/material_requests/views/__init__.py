"""The requester's screens (plan §11 M4.3): new request, materials, review, status, "My requests".

Flow: home → new request → project → materials → review → submit → status page. Reads go
through ``apps.material_requests.selectors``; writes through ``apps.material_requests
.services`` from POST-only views (``@mutating``), each on its own URL, with its GET page
on another. Every change of a draft answers an htmx request with the refreshed lines (the
page still works without JavaScript: the answer is then a redirect). The order of the
checks is S7's: the capability somewhere (403), the request in scope (404), the capability
on it (403). The services authorize again inside their transaction.
"""

from apps.material_requests.views.closing import (
    line_close_short,
    line_reopen,
    request_close,
    request_reopen,
    request_schedule,
)
from apps.material_requests.views.editing import (
    line_apply_add,
    line_apply_edit,
    line_edit,
    line_new,
    line_remove_reviewed,
)
from apps.material_requests.views.requester import (
    line_add,
    line_add_free,
    line_remove,
    line_update,
    request_cancel,
    request_detail,
    request_list,
    request_materials,
    request_new,
    request_review,
    request_start,
    request_submit,
    request_withdraw,
)
from apps.material_requests.views.reviewer import (
    inbox,
    line_decide,
    line_link,
    line_reduce,
    line_resolve,
    line_sourcing,
    request_approve_all,
    request_assign,
    request_cancel_any,
    request_examine,
    request_return,
)
from apps.material_requests.views.trace import project_materials, request_ledger

__all__ = [
    "inbox",
    "line_add",
    "line_add_free",
    "line_apply_add",
    "line_apply_edit",
    "line_close_short",
    "line_decide",
    "line_edit",
    "line_link",
    "line_new",
    "line_reduce",
    "line_remove",
    "line_remove_reviewed",
    "line_reopen",
    "line_resolve",
    "line_sourcing",
    "line_update",
    "project_materials",
    "request_approve_all",
    "request_assign",
    "request_cancel",
    "request_cancel_any",
    "request_close",
    "request_detail",
    "request_examine",
    "request_ledger",
    "request_list",
    "request_materials",
    "request_new",
    "request_reopen",
    "request_return",
    "request_review",
    "request_schedule",
    "request_start",
    "request_submit",
    "request_withdraw",
]
