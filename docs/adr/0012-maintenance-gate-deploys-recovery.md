# ADR-0012: Deploys behind a maintenance gate; forward fixes; restores reconciled; append-only backups

- **Status:** Accepted
- **Date:** 2026-10-26

## Context

Concurrent writers during a migration, or a restored database that resurrects revoked access or unsent email, are the main operational risks.

## Decision

- Deploys are serialized behind a maintenance gate (`flags/maintenance.on`): writers stopped, the worker paused, internal verification, then traffic and processing enabled together. Both images are deployed by signed digest.
- Forward fixes are preferred to rollbacks; migrations are expand/contract and reversible, or flagged.
- A restore is followed by a security, MFA and outbox reconciliation (Q23, Q25).
- Backups: snapshot-consistent `pg_dump` plus a manifest every 6 h with a read-only role (sessions and cache excluded), a restic offsite copy with **append-only** credentials on the VPS, a 90-day protected retention pruned from the maintainer's machine, and a quarterly restore drill with no outbound connectivity.

## Consequences

- A deploy takes a short, planned write outage (Q21).
- A compromised VPS cannot delete its own offsite backups.
