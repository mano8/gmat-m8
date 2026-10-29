# ADR-0009: No Redis or broker: security DatabaseCache, durable email outbox and worker

- **Status:** Accepted
- **Date:** 2026-10-26

## Context

Rate limits need atomic counters that never evict live entries. Email must never block a request, and a lost or duplicated supplier email has real cost.

## Decision

- A security cache backend built on Django's `DatabaseCache`: atomic `add` / `incr`, no eviction of live entries, fails closed (429) when full. It backs the allauth rate limits.
- All mail goes through a durable `EmailOutbox`, sent by a `worker` process built from the same image. Recipients are stored as user ids and re-checked at claim time. Ambiguous dispatches are never resent automatically.
- General background jobs are backlog item B11.

## Consequences

- Everything lives in PostgreSQL, so backups and restores cover it.
- The worker is paused during deploys and restores, and its pause flag is part of the runbooks.
