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
- [ADR-0014](0014-engineer-role-review-workflow.md): Engineer role, reviewer assignment, reviewer edits and self-decisions
- [ADR-0015](0015-upload-scanning-and-decoder-isolation.md): Uploads are scanned by clamd, and decoded in a container of their own
- [ADR-0016](0016-restores-hold-what-can-hand-over-an-account.md): A restore holds what can hand over an account
- [ADR-0017](0017-signed-release-facts-and-public-image-packages.md): The VPS decides only on the signed manifest; image packages are public
- [ADR-0018](0018-procurement-branch-change-from-now-on.md): A project's change of procurement branch applies from now on
- [ADR-0019](0019-self-decision-covers-return-and-reduce.md): Returning or reducing one's own request is a self-decision
- [ADR-0020](0020-receipt-void-may-revive-pending-on-stock-line.md): A receipt void may revive pending purchase on a line switched to stock
- [ADR-0021](0021-who-reconciles-a-restore.md): Who reconciles a restore, and which accounts it suspends
- [ADR-0022](0022-no-change-to-ones-own-authority-or-email.md): An administrator never changes their own authority or email
- [ADR-0023](0023-lists-page-in-the-database.md): Lists page in the database; lists worked out from the ledger use a cursor
- [ADR-0024](0024-session-idle-limits-own-sessions-credential-notices.md): Idle limits, one's own sessions and credential-change notices
- [ADR-0025](0025-ci-test-shards.md): CI runs the tests in four shards at once; time-dependent tests run alone
