# ADR-0011: Full authorization inside every write transaction; explicit principals; generations

- **Status:** Accepted
- **Date:** 2026-10-26

## Context

A permission checked at request time can be revoked before the write commits. Background work and management commands also act, and must not borrow a user's session.

## Decision

- Services take a **principal**: `SessionPrincipal`, `TokenPrincipal` (one operation), `SystemPrincipal` (a worker without business capability) or `OperatorPrincipal` (commands run with `--operator <email>`). Only allowlisted modules construct non-session principals.
- Every mutating service runs `authorize_in_txn(principal, ...)` inside its transaction, in full each time, never skipped for an unchanged generation.
- `User.authz_generation` and the access state generations serialize revocations against in-flight writes.
- Write order: `transaction.atomic()` with `lock_timeout`, authorize, idempotency record, locks in canonical order, recompute, validate, write, reconcile, audit and publish. Deadlocks are retried only through one shared wrapper.
- Idempotency keys are signed, valid 24 h and bound to actor, operation and payload hash; a replay re-authorizes.

## Consequences

- A revoked membership, a deactivated user or an expired token cannot complete a write that started earlier.
- Services never import views, forms or the request, so a later API reuses them unchanged.
