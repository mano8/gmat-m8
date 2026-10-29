# Architecture decision records

Accepted decisions. An ADR wins over the planning notes. A new decision gets the next number; a changed one is superseded by a new ADR.

- [ADR-0001](0001-modular-monolith.md): Modular monolith: Django 6.1 + HTMX + PostgreSQL 18
- [ADR-0002](0002-shared-schema-scope-policy.md): Shared-schema multi-company with a code-defined scope policy
- [ADR-0003](0003-one-deployment-per-group.md): One deployment per unrelated business group
- [ADR-0004](0004-session-auth-allauth-mfa.md): Session authentication with django-allauth (>= 65.20.1) and MFA
- [ADR-0005](0005-derived-fulfilment-reconciled-commitments.md): Approval is stored; fulfilment is derived; commitments are reconciled
- [ADR-0006](0006-receipt-attributions-direct-handovers.md): Receipts are attributed to allocations; direct-to-site receipts create linked handovers
- [ADR-0007](0007-uv-universal-lock.md): uv with a universal, hash-verified lock
- [ADR-0008](0008-caddy-whitenoise.md): Caddy and WhiteNoise; no Node.js at runtime
- [ADR-0009](0009-no-redis-database-cache-outbox.md): No Redis or broker: security DatabaseCache, durable email outbox and worker
- [ADR-0010](0010-business-rules.md): Business rules: H0.1 answers and working assumptions
- [ADR-0011](0011-authorize-in-transaction-principals.md): Full authorization inside every write transaction; explicit principals; generations
- [ADR-0012](0012-maintenance-gate-deploys-recovery.md): Deploys behind a maintenance gate; forward fixes; restores reconciled; append-only backups
- [ADR-0013](0013-agent-contributions.md): Agent contributions: interim single-account setup, protected tags, signed releases
