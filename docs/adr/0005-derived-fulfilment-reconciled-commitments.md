# ADR-0005: Approval is stored; fulfilment is derived; commitments are reconciled

- **Status:** Accepted
- **Date:** 2026-10-26

## Context

Mixing approval and fulfilment in one status list does not describe partial approvals, partial receipts or reversals. Stored counters drift when a document is voided.

## Decision

- Approval status is stored per line. Received, pending, draft-reserved, delivered, committed, awaiting-handover, to-purchase and outstanding quantities are **derived** from documents, never stored.
- Quantities are `Decimal(12,3)`, prices `Decimal(14,4)`; never float.
- Pending is a waterfall over open allocations in attribution order, capped by what the line can still receive. Draft allocations reserve demand.
- `committed = max(D, R) + P + Q <= approved`. Any change to an input reconciles the whole affected component: discover, lock, verify, apply, repeat until stable.
- Closures record their provenance (`LINE` or `REQUEST`). Voiding a posted document can reopen the documents it affected.
- State changes go through services checked against `ALLOWED_TRANSITIONS`; no FSM library.

## Consequences

- Reconciliation is the most delicate code in the system and is covered by property-based and concurrency tests.
- The database holds only facts (documents), so a void or an amendment cannot leave a stale counter.
