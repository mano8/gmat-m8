# ADR-0006: Receipts are attributed to allocations; direct-to-site receipts create linked handovers

- **Status:** Accepted
- **Date:** 2026-10-26

## Context

Goods arrive at a warehouse or directly at a worksite. "Do not count a site receipt twice" must hold structurally, not by convention.

## Decision

- A goods receipt is split into `ReceiptAttribution` rows, one per allocation it serves.
- Every attribution is capped by `A - R` (and `A - D` on site); the excess is surplus. `R <= A` and `D <= A` always.
- Each site attribution creates exactly one linked `DIRECT_RECEIPT` handover line.
- Posted receipts and handovers are immutable: void with a reason and re-enter. A `DIRECT_RECEIPT` handover is voided only through its receipt.

## Consequences

- Handed-over quantity on site cannot exceed what was received, and a void can reopen the documents it touched.
- Warehouse stock is not tracked in phase 1, so a warehouse handover over the received quantity is a warning only; the hard limit is the approved line quantity.
