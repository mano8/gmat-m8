# `apps/receiving`

What this app holds and how its parts fit. Loaded when you work in this directory; the
rules that hold everywhere are in the root `CLAUDE.md`. Keep this file current when the
app changes.

## Layout

- `GoodsReceipt` (number `REC-…` per branch at posting; where the goods arrived; posted or voided)
- `GoodsReceiptLine` (accepted, rejected, incident, surplus; C1 composite keys)
- `ReceiptAttribution` (per allocation; C2 composite keys), a trigger freezing posted receipts except their void (`receiving.0002`)
- `transitions`
- `attribution` (pure: candidates, caps A - R and A - D on site, default split in attribution order, manual split)
- `ledger` (the `ReceiptSource` purchasing reads: posted receipts only)
- `services` (`post_receipt`: authorized on the order at a warehouse or on the site project's requests at a worksite (Q9, every role in scope since Q44/ADR-0028; without `po.view` only the project's order lines), component locked L2-L5, C7/C10/I4, numbers at L9, direct handover, automatic close of requests and order via `_settle_closure`/`_settle_order`, reconcile; `void_receipt`: L6 then L7, reopen, reconcile)
- `selectors` (`expected_deliveries(ctx, project)`: the §3.4 projection for site managers, and `ExpectedOrders` (the screen's cursor source: orders soonest first, filtered in SQL); M6.3: receipts and their lines, `receivable_orders` (by number, supplier snapshot or project), `receipt_order` (an order of the user's branches, or an issued/closed one that buys for a project they see), `receipt_locations` (C7), `receipt_rows` (the form's lines: suggested quantity and default split from the pure `attribution`, the outstanding quantity only for `po.view`), `expected_projects`), read-only admin, and the screens under `/receiving/` (M6.3: `views/` (`pages` read-only, `actions` the POST-only post and void), `forms` (`ReceiptForm`: only the lines and allocations offered exist as fields, places from the selector), `urls`, `templates/receiving/`): receipts list `/receiving/` (`receipt.view`), receipt page `<id>/` (lines, attributions, void dialog for `receipt.void`)
- `orders/` (`receipt.post`: find the order)
- `orders/<id>/receive/` (the mobile form: quantities prefilled, incident block, delivery note, adjustable split) → `orders/<id>/post/` (idempotent; a double tap that used the order up leads to the same receipt)
- `expected/` and `expected/<project>/` (the site manager's §3.4 projection, grouped by order, with the way in to the form at the project's worksite)
- `<id>/void/`

## Project receiving screens (M9.2)

The project page offers "Receive materials" (to the project's expected deliveries, then the receipt form); `selectors.receives_by_order(ctx)` is false for who holds only site grants, who is never offered the order search (the receipts list and the project list lead to the projects, the form's breadcrumb returns to the project).

## Rules in detail

The full text of the root `CLAUDE.md`'s rules this directory implements; the root keeps a one-sentence summary of each.

- Project receiving (Q44, ADR-0028): every business role posts receipts at the worksite of a project in its membership's scope, whoever requested the goods. `receipt.view`/`receipt.post` are `Reach.SITE` (○ site) for site managers, technicians and partners (documents only through their project; no branch row, warehouse or order), and for these `SITE_CAPABILITIES` the branch roles' grants also reach their projects' worksite documents. Warehouse receiving and `receipt.void` stay with the order's branch; a project receiver without `po.view` names only the project's lines and allocations. Its screens: `apps/receiving/CLAUDE.md`.
