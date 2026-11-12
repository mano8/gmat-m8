# ADR-0028: Every business role receives at the worksites of its projects

- **Status:** Proposed (M9.1, the maintainer's decision Q44 of 2026-11-04, issue #32; accepted when the phase PR merges)
- **Date:** 2026-11-12
- **Governs:** `apps/access/roles.py`, `apps/access/scopes.py`, `apps/receiving/services.py`, `apps/receiving/selectors.py`
- **Amends:** [ADR-0010](0010-business-rules.md) (Q9), plan §3.4 (the `receipt.view` and `receipt.post` rows of the capability matrix and the notes on technicians, partners and site managers)

## Context

Goods often arrive at a worksite when nobody from purchasing or the warehouse is there. Until now only group administrators, material managers, engineers, warehouse operators and the project's site manager could post a receipt there (Q9). A worksite receipt was authorized on the requests of that project that the order serves, and technicians and partners see only their own requests, so giving them `receipt.post` alone would not have been enough. It would also have let them receive only goods they had requested themselves.

The maintainer decided Q44 on 2026-11-04: all seven business roles can post receipts at the worksite of a project within their membership's scope, whoever requested the goods. Technicians use the active projects of their branch scope, site managers their valid manager assignments, partners their valid partner assignments on active projects while their company is active. A branch also receives for a project it executes but does not buy for. Warehouse receiving and voiding keep their policy.

## Decision

### 1. The site reach (`Reach.SITE`, ○ site)

A new reach in `apps/access/roles.py`. A grant with it reaches a document **only through its project**: the projects of the membership's scope (S3 for branch roles, S4 for assigned ones), active only for technicians and partners (`ACTIVE_PROJECTS_ONLY`), and never a document by its branch. In `apps/access/scopes.py`:

- `Grant.document_q` matches a site grant's documents by the document's project path only. A document without a project (a purchase order, a warehouse receipt) is never one;
- `Scope.branch_q` ignores site grants: they reach no branch row and no warehouse;
- `Scope.project_q` keeps them: they reach the projects and their worksites.

### 2. The capability matrix

| Capability | GROUP_ADMIN | MATERIAL_MANAGER | WAREHOUSE_OPERATOR | SITE_MANAGER | TECHNICIAN | PARTNER_REQUESTER | ENGINEER |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `receipt.view` | ● | ● | ● | ○ site | ○ site | ○ site | ● |
| `receipt.post` | ● | ● | ● | ○ site | ○ site | ○ site | ● |
| `receipt.void` | ● | ● | — | — | — | — | ● |

The site manager's cells were "○ assigned" in the code, which gave the same rows; they are now the site reach, so the cell and its meaning agree. No other capability changes: technicians and partners keep `request.view` own only, and get no `po.view`, `price.view`, `supplier.view` or `handover.*`.

### 3. Branch roles also reach their projects' worksites (`SITE_CAPABILITIES`)

`SITE_CAPABILITIES = {receipt.view, receipt.post}`. For these two capabilities, every grant (● included) reaches, on top of its own documents, the documents at the worksite of every project in its scope. So a warehouse operator or material manager of the branch that executes a project receives on its site and reads those receipts back, even when another branch buys for it. A receipt stays a document of its order's branch (S5): `receipt.void` is not in the set, so only the order's branch voids it, and warehouse receipts are reached by the order's branch only.

### 4. Authorizing a worksite receipt

`post_receipt` authorizes on the same objects as before: the order at a warehouse; at a worksite, the requests of that project the order serves. With the reach above, `receipt.post` covers those requests through their procurement branch (which is the order's, C5) or through their project. Every request served shares that project and that branch, so one membership covers them all or none (S6). The requester plays no part. The check runs in full in the write's transaction on every call, an idempotent replay included: memberships, assignments, the project's, branch's and company's state and the session epoch are read again (S8-S10).

### 5. What a project receiver may name

- **The worksite** of a project in their scope, from an order with a live allocation for that project (C7 as before). A warehouse, or a worksite outside their scope, is refused (404).
- **The project's allocations** only, even when the order buys for several projects (as before: a site receipt attributes to the site's project; a manual split naming another project's allocation is refused).
- **The order lines that serve the project:** whoever does not see the whole order (no `po.view` on it, read at the transaction's clock after the authorization) may receive only a line that carries a live allocation of the project. Any other line is answered "A line is not on this order", before anything about it is checked, so the refusal tells nothing. Whoever sees the order may still record such a line as surplus at the site: goods delivered to the wrong place (worked example E12).

The ledger rules do not change: order state (C10), the over-receipt check (I4), the caps A − R and A − D, the lock order, the reconciliation, immutable receipts and exactly one linked `DIRECT_RECEIPT` handover, whose `handed_by` and `received_by` are the receiver.

### 6. The projection

Reads go through `receipt.view` and its site reach: the expected deliveries of a project (§3.4's projection: order number, supplier display name, expected date, material, unit, what that project still expects), the receipt form at the worksite (the order line's outstanding quantity only for `po.view`), and the worksite receipts of their projects. Technicians and partners still never see a price, a supplier's contact details, another project's quantity, the whole purchase order, a handover document, or another person's request beyond the request number an allocation names. The screens (M9.2) make that reachable and hide the rest.

### 7. The screens (M9.2)

- **Discovery.** The page of a project offers "Receive materials" to whoever holds `receipt.post` on it (the project not closed); it leads to the project's expected deliveries, from which the receipt form is reached. A reader without `receipt.post` keeps "Expected deliveries". The project's expected-deliveries page links to its worksite receipts (the receipts list narrowed to the worksite), and offers "Receive these goods" only where the receipt form would accept the worksite (M9.3): not for a closed project, whose goods arrive at a warehouse, nor at the worksite of an inactive branch or company (S9).
- **Order search is for branch grants.** `selectors.receives_by_order` is true when some `receipt.post` grant is not `Reach.SITE`. Whoever has only the site reach (site managers, technicians, partners) is never offered the order search: the receipts list and the project list lead to the projects, the order search page says so (it lists nothing for them and shows no search), and the receipt form's breadcrumb returns to the project's expected deliveries.
- **Nothing new is exposed.** The screens read through the selectors above; the receipt page shows the handover and the purchase order only to `handover.view` / `po.view`, and a receipt's files follow the receipt (X8).

## What this changes

- **ADR-0010 Q9:** "material manager or warehouse operator, or the project's site manager through the expected-deliveries view" becomes: every business role at the worksite of a project in its membership's scope (§5 above), and the order's branch at its warehouses.
- **Plan §3.4:** the matrix rows above; the note "technicians and partners see delivery dates and quantities only through their own request's detail page, never handover or receipt documents" keeps holding for handovers and for receipts outside their projects' worksites.
- **Attachments:** a receipt's files follow the receipt (`receipt.view` to read them, `receipt.post` to add one to a posted receipt), so a project receiver reads and adds the files of their projects' worksite receipts. The exception register entry X8 in `docs/security.md` covers them.

## Consequences

- More people can post receipts, and each receipt names who posted it (the audit's actor, `received_by`). A wrong receipt is voided by the order's branch, as before.
- Branch roles see more receipts: those at the worksites of the projects they execute.
- A dashboard panel or menu entry gated by `receipt.view` is now offered to technicians and partners; M9.2 decides what each shows.
- A worksite receipt's lines from an actor without `po.view` are restricted to the project's lines, which a site manager could receive before without restriction (as surplus). That is a narrowing for site managers, consistent with §3.4's projection.
