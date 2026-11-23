# `apps/purchasing`

What this app holds and how its parts fit. Loaded when you work in this directory; the
rules that hold everywhere are in the root `CLAUDE.md`. Keep this file current when the
app changes.

## Layout

- `PurchaseOrder` (number at issue per buyer company, revision, snapshots)
- `PurchaseOrderLine` (C9 composite FK, one open line per material, optional prices)
- `PurchaseAllocation` (ceiling `qty`: grows via C5, shrinks via audited trims)
- `transitions`
- `receipts` (the `ReceiptSource` seam receiving registers in M6.1: received per line, attributed per allocation)
- `waterfall` (pure: attribution order, P/Q, line ledgers, `Book`)
- `ledger` (DB loaders, the request-line `FlowSource` for R/P/Q/allocated, the deactivation provider)
- `components` (`Component.lock`: discover → lock L2-L5 → verify, 200-node bound, `fixpoint` with effective-pending cuts, `trim`, `reconcile`, `TrimReason`; M6 reuses it)
- `dispatch` (M5.3 supplier dispatches; M14.4: `mode()` → `DispatchMode` `off`/`manual`/`on` (ADR-0010 Q7), `queue_order` at issue/amend supersedes older queued revisions under `manual` and `on` and queues only under `on`, `queue_revision` the one maker of a `PO_ORDER` row (stable `Message-ID` for a revision's first dispatch, a fresh one for a repeat), `cancel_dispatches` + the unique `PO_CANCELLATION` notice, content from the snapshots via `templates/purchasing/email/`, the worker's `OrderDispatches` source registered in `apps.py`: order `FOR SHARE` at L4, eligibility, the obligation, every attempt audited on the order)
- `services/` (`drafts`: `create_po`, `update_order`, `add_request_lines`, `allocate`, `increase_allocation`, `unallocate`, `update_line`, `remove_line`; `orders`: `issue` with revalidation, `amend`, `cancel`, `close_line_short`; `common`: C5/C6/S9 rules, snapshots, `_settle_order` for automatic close/reopen; `dispatches`: `confirm_dispatch`/`resend_dispatch` for an `AMBIGUOUS`/`FAILED` dispatch, M14.4's `send_order` (an issued order to its supplier's current address, idempotent), order L4 → rows L4b)
- `selectors/` (a package: `purchase_orders`: orders, lines with prices only for `price.view`, allocations, `drafts`, `stale_drafts`, receipt `progress`; `worklist`: `to_purchase`, `by_material`, `unallocated_on_order`, the builder's `ship_to_choices` and `candidates`; `order_history`: `history` from the audit trail for `po.manage`/`audit.view`, the order's `dispatches`, a request's trims), read-only admin without prices, and the screens under `/purchasing/` (M5.2: `views/` (`pages` read-only, `actions` one POST-only URL per service call, `common`), `forms` (choices from selectors; `ItemsMixin` = a tick and a quantity per request line, only the lines offered exist as fields; `AmendForm` sends only what differs), `urls`, `templates/purchasing/`): the worklist `/purchasing/` (`po.manage`: by material, project/priority/supplier filters, reserved-in-drafts, stale drafts and ordered-for-nobody flags, ticks → `new/` → `create/`), the orders list `orders/` (`po.view`: pending by default, receipt progress), the order page `orders/<id>/` (one URL for every state; the builder's tools on a draft, `amendment/`, close-short and cancel on an issued order, allocation breakdown, history) and `orders/<id>/print/` (from the snapshots; print CSS in `app.css`, `data-print` button in `app.js`)
- `dispatches/<id>/confirm|resend/` (M5.3, the order page's dispatch panel)
- M14.4: `orders/<id>/send/` (the head's *Send to the supplier* inline dialog, `order_dialogs.send_dialog` with `send_body.html`)
- `dispatches/<id>/delivery/` (GET-only, `@session_passive`, `po.view`: one dispatch's status, `dispatch_status.html`, polled with `data-poll` every 5 s while queued or sending, 60 s once delayed)
- `views/dispatch_messages.py` (the flashes of issue, amend, send and cancel)
- `dispatch_before.html` (the issue bar's and amendment page's notice)
- warehouse operators read orders and the print page without prices

## Supplier dispatch modes (ADR-0010 Q7, M14.4)

`PO_DISPATCH` is `off` (print only, the default when absent), `manual` (a buyer's *Send to the supplier* only: `services.send_order`, POST-only `purchasing:send_order`, `po.manage`, the supplier's **current** address shown before sending, refused while the revision is queued or sending, a fresh `Message-ID` for a repeat) or `on` (also at issue and amendment, to the address as issued), anything else refuses to start (ADR-0010 Q7, M14.4);
