# ADR-0020: A receipt void may revive pending purchase on a line switched to stock

- **Status:** Accepted (the maintainer's answer to M8.1's open question; recorded in PR #33)
- **Date:** 2026-11-08 (recorded); decided with M8, merged 2026-11-07
- **Complements:** ADR-0005

## Context

ADR-0005: approval is stored, fulfilment is derived, and voiding a posted document can reopen the documents it affected. A receipt void revives the pending quantity of the allocations it had served, and reconciliation trims what goes beyond the need (examples E4 and E8).

A request line's sourcing can be changed from `PURCHASE` to `STOCK` after its goods arrived. Rule C5 checks that a line is *to purchase* when an allocation is created or increased, not when a void revives pending on an existing allocation. M8.1's state machine found the case: a void on such a line brings pending back on a line now sourced from stock. No rule of the plan forbids it, and the ledger stays right.

## Decision

- **Kept as is.** A receipt void may revive pending purchase on a line whose sourcing is now `STOCK`. The C5 *to purchase* condition applies when an allocation is created or increased, not when a void restores a ceiling that already exists.
- The revived pending is a commitment like any other. When the line is covered another way, the usual reconciliation cuts it: a handover from stock raises `D`, `max(D, R) + P + Q` goes above the approved quantity, and the excess pending is trimmed (an audited `EXCESS_COMMITMENT` trim).
- Nothing is cut at the void itself. The line can still be bought on the existing order if that order receives again.

## Consequences

- No special case in the void path or in reconciliation.
- The invariant checker (`tests/invariants.py`, C5) treats *to purchase* as part of the increase, not as a property of every live allocation; `tests/integration/test_ledger_machine.py` runs the case, including its fixed tour "E12 with stock handed over first".
- A future inventory module that reserves stock for a line (a hold) may cut revived pending the same way a handover does.
