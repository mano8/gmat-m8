# ADR-0003: One deployment per unrelated business group

- **Status:** Accepted
- **Date:** 2026-10-26

## Context

The shared schema (ADR-0002) isolates companies inside one group. Unrelated business groups have no reason to share a database, an operator or an incident.

## Decision

Each unrelated business group gets its own deployment: its own stack, database, secrets and backups. Companies and branches inside one group share a deployment.

## Consequences

- A breach or an outage never crosses groups.
- Operations repeat per group; the compose stack is small enough that this is acceptable (Q18, accepted).
