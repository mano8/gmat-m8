# ADR-0019: Returning or reducing one's own request is a self-decision

- **Status:** Accepted (the maintainer's decision Q1 of M8.3; implemented in PR #33)
- **Date:** 2026-11-08 (recorded); decided with M8.3, merged 2026-11-07
- **Amends:** ADR-0014 §3

## Context

ADR-0014 §3 lets the self-deciding roles (`GROUP_ADMIN`, `MATERIAL_MANAGER`, `ENGINEER`) decide their own requests through a grant of that role on the request, with session MFA, and marks those decisions `self_decided: true` in the audit. It names approving and rejecting lines, and M7b.3 adds approving one's own edits.

Two other reviewer actions change what a request asks for: sending it back to draft (`return_to_draft`, which clears every decision) and lowering an approved quantity (`reduce_approved_qty`). Before M8.3 they checked `request.review` only, so a requester with that capability through any role could do them on their own request.

## Decision

- `return_to_draft` and `reduce_approved_qty` on a request the actor submitted follow the same rule as a decision (`_require_may_decide` in `apps/material_requests/services/review.py`). Either the procurement branch sets `allow_self_approval`, or a membership of a self-deciding role grants `request.review` on that request, with session MFA (`authorize_self_decision`).
- The audit event (`request.returned`, `request_line.reduced`) carries `self_decided: true` when the actor is the requester, and no marker otherwise.
- On someone else's request nothing changes.

## Consequences

- A site manager who is also the requester cannot return or reduce their own request unless the branch allows self-approval, which is the same as for a decision.
- Every action that changes what one's own request asks for is visible as a self-decision in the audit.
- Tests: `tests/unit/material_requests/test_review.py` (return and reduce by the requester, the `self_decided` marker).
