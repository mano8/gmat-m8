"""Material request lifecycle: requester, reviewer and material manager (plan §4.3 to §4.9).

Each service runs in one transaction with ``lock_timeout`` through the shared retry
wrapper, in the order of CLAUDE.md "Writes":

1. ``authorize_in_txn`` (L0 ``FOR SHARE``, in full, every time) on the request, with
   ``request.view`` as the read capability (404 outside it, 403 without the operation's);
   a project is authorized with ``project.view`` as its read capability;
2. the idempotency record of a create or a submit (L1);
3. the request header ``FOR UPDATE`` (L2), re-read and compared with what was authorized
   (a change in between retries the transaction: discover → lock → verify); every change
   to a line locks its header first, so a submit and an ``add_line`` serialize;
4. the line ``FOR UPDATE`` (L3) for a change to one line;
5. the document number last (L9), so a rollback leaves no gap.

Capabilities (plan §3.4): ``request.create`` (own drafts: create, edit, submit, discard;
notes of an own approved request), ``request.withdraw`` (own submitted request back to
draft, or cancelled, before any decision), ``request.cancel_any`` (anyone's, nothing
allocated or delivered), ``request.review`` (decide lines, return for changes, reduce an
approved quantity; never on one's own request unless the procurement branch allows
self-approval) and ``request.source`` (sourcing, non-catalog resolution, priority and
needed-by, closing and reopening). A reviewer is assigned at submit by the requester or
afterwards by any reviewer in scope (``assignment``, M7b.2); the assignee's ``User`` row is
locked at L0 with the actor's, before ``authorize_in_txn``. A requester changes material,
unit and quantity only in ``DRAFT`` (§4.4); ``request.edit`` holders edit the lines of
submitted and approved requests (``editing``, M7b.3, ADR-0014 §5), each edit a revision
against the as-submitted copy that every submit records. Every request goes through review
(Q4, ADR-0010), with no automatic approval; a decision or an edit sent with the page's
``seen_version`` is refused once the request was edited or resubmitted meanwhile.

Ledger inputs (§4.5): closing or reopening a line or a request, reducing an approved
quantity and changing the priority or needed-by date change the attribution order or the
open lines, so they lock the request's whole allocation component before any line
(``commitments``: every request header at L2, every request line at L3, then the purchase
orders at L4/L5), trim the allocations of the lines they close, and end with the
component's reconciliation (purchasing, M5.1), after the automatic close or reopen. The
quantities they check (D, R, P, Q) come from ``ledger``; automatic close and reopen follow
§4.9.

C9: a catalog line carries its material's unit through the composite foreign key. A
material whose unit changed between reading it and inserting the line fails that key: the
line is refused (its quantity was meant in the old unit), never silently converted.
"""

from apps.material_requests.services.assignment import assign_reviewer
from apps.material_requests.services.common import _lock_header as _lock_header
from apps.material_requests.services.common import _settle_closure as _settle_closure
from apps.material_requests.services.editing import (
    LineEdit,
    add_line_as_reviewer,
    edit_line,
    remove_line_as_reviewer,
)
from apps.material_requests.services.requester import (
    add_line,
    cancel,
    create_draft,
    remove_line,
    submit,
    update_line,
    update_request,
    withdraw,
)
from apps.material_requests.services.review import (
    LineDecision,
    approve_all,
    change_needed_by,
    change_priority,
    change_schedule,
    close_line_short,
    decide_line,
    decide_lines,
    manual_close,
    reduce_approved_qty,
    reopen,
    reopen_line,
    resolve_non_catalog_line,
    return_to_draft,
    set_sourcing,
)

__all__ = [
    "LineDecision",
    "LineEdit",
    "add_line",
    "add_line_as_reviewer",
    "approve_all",
    "assign_reviewer",
    "cancel",
    "change_needed_by",
    "change_priority",
    "change_schedule",
    "close_line_short",
    "create_draft",
    "decide_line",
    "decide_lines",
    "edit_line",
    "manual_close",
    "reduce_approved_qty",
    "remove_line",
    "remove_line_as_reviewer",
    "reopen",
    "reopen_line",
    "resolve_non_catalog_line",
    "return_to_draft",
    "set_sourcing",
    "submit",
    "update_line",
    "update_request",
    "withdraw",
]
