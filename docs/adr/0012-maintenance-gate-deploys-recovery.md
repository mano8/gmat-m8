# ADR-0012: Deploys behind a maintenance gate; forward fixes; restores reconciled; append-only backups

- **Status:** Accepted
- **Date:** 2026-10-26
- **Governs:** `docker_compose/gmat_m8/deploy.sh`, `docker_compose/gmat_m8/recover.sh`, `docker_compose/gmat_m8/restore-drill.sh`, `docker_compose/gmat_m8/ops/`, `docker/backup/`, `docker_compose/offsite/`

## Context

Concurrent writers during a migration, or a restored database that resurrects revoked access or unsent email, are the main operational risks.

## Decision

- Deploys are serialized behind a maintenance gate (`flags/maintenance.on`): writers stopped, the worker paused, internal verification, then traffic and processing enabled together. Both images are deployed by signed digest.
- Before the gates go on, a deploy refuses env files that are not mode 0600, an empty offsite repository and, only while `DJANGO_ADMIN_ENABLED` turns the optional admin on, an admin allowlist (`ADMIN_ALLOWED_CIDRS`) that is empty, open or overlaps Docker's bridge ranges (`ops/checks.py admin-cidrs --enabled`; *added 2026-11-14 by [ADR-0030](0030-administration-in-the-app.md)*, M10.5; until then the allowlist was always required).
- Forward fixes are preferred to rollbacks; migrations are expand/contract and reversible, or flagged.
- A restore is followed by a security, MFA and outbox reconciliation (Q23, Q25).
- Backups: snapshot-consistent `pg_dump` plus a manifest every 6 h with a read-only role (sessions and cache excluded), a restic offsite copy with **append-only** credentials on the VPS, a 90-day protected retention pruned from the maintainer's machine, and a quarterly restore drill with no outbound connectivity.

## Consequences

- A deploy takes a short, planned write outage (Q21).
- A compromised VPS cannot delete its own offsite backups.
