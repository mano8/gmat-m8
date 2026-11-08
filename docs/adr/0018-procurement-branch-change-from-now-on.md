# ADR-0018: A project's change of procurement branch applies from now on

- **Status:** Accepted (the maintainer's decisions of 2026-11-07; implemented in PR #37)
- **Date:** 2026-11-07
- **Governs:** `apps/projects/procurement.py`, `apps/material_requests/procurement.py`

## Context

The project's procurement branch buys for it, and its company is the buyer on the purchase order (ADR-0010 Q2). A request records the procurement branch it was submitted with. Three rules depend on that branch:

- an order buys only for requests of its own branch (C5/C6);
- an order's worksite destination is checked against the project's **current** procurement branch (C7);
- a receipt belongs to its order's branch, and a handover to the request's procurement branch (C8).

A project's procurement branch can be changed. Before this decision, a request submitted before the change kept the old branch. The new branch could not order for it ("bought by another branch"), and the old one could not ship to the worksite. The request was stuck.

Rewriting everything to the new branch is not an option either:

- An issued order binds its buyer company (its `PED-…` number, the buyer snapshot, the supplier's invoice).
- Received goods sit in the old branch's warehouses.
- Posted receipts and handovers are immutable (triggers).
- One order can serve requests of several projects.

## Decision

**A change of procurement branch means "from now on".** What the old branch ordered, received or handed over stays its own. Only what nothing commits moves.

- **Open requests follow the project.** The project's `DRAFT`, `SUBMITTED` and `APPROVED` requests move to the new branch in the same transaction as the project change. Each move is audited (`request.procurement_branch_changed`, with the request's previous branch and a `cause`).
- **The change is refused while one of them is committed.** A request is committed when any of its lines has a flow: allocated, reserved in a draft, pending, received or delivered. The refusal lists those requests. To change the branch anyway, finish them or close them short first.
- **Rejected, cancelled and closed requests keep their branch** when the project changes.
- **A closed request reopened by hand catches up** with the project's current procurement branch, if nothing is committed (cause `reopen`).
- **A reopened request that is committed keeps its old branch.** That branch finishes it: it buys the rest to its own warehouses (or those of branches it manages) and hands it over from the request's page (the project's handover page lists only the current branch's requests). Its new orders cannot name the worksite as their destination, which C7 keeps for the project's current procurement branch; an order already issued still receives there. Moving the request would break C8: unless the new branch manages the old one, goods at the old branch's warehouse could no longer be handed over, and neither could goods put back by a later void.
- **Partly committed requests are not split** (the rest into a new request of the new branch). The case is rare. The manual path is to close the rest of the line short and raise a new request.
- **Past orders, receipts and handovers are never rewritten.**

## Consequences

- A request can no longer be stuck between two branches by a project change.
- Changing a project's procurement branch while it has committed requests takes a human step: finish or close them first.
- A committed request reopened after a change stays with the old branch. It is on the old branch's worklist, and on the new branch's only when that branch's scope also reaches the old one (it manages it, or a grant reaches both).
- The followers are registered from the requests app (`apps.projects.procurement`), because projects do not import requests. A future app holding per-project documents that depend on the branch registers there as well.
