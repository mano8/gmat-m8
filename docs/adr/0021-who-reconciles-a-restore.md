# ADR-0021: Who reconciles a restore, and which accounts it suspends

- **Status:** Accepted (the maintainer's decisions of 2026-11-06, M8.4; implemented in PR #33)
- **Date:** 2026-11-08 (recorded)
- **Complements:** ADR-0012, ADR-0016
- **Governs:** `apps/recovery/`, `docker_compose/gmat_m8/ops/failed-state.sql`

## Context

ADR-0012: a restore is followed by a security, MFA and outbox reconciliation (Q23, Q25). ADR-0016: a restore holds what can hand over an account. Neither says who runs the reconciliation, and the plan said "reconcile: superuser". `post-restore-reconcile` ends every session, revokes pending tokens, deletes recovery codes, quarantines the outbox and either replays the failed database's access events or, without one, suspends accounts (Q23). Whoever runs it decides the state the installation reopens in.

An installation's first administrator can be created without `--superuser` (`bootstrap_admin`). Such an installation has no platform operator, and a restore must still be possible.

## Decision

- **A platform operator runs it**: an active superuser account (`bootstrap_admin --superuser`) whose memberships grant `org.manage`, named with `--operator` (`OperatorPrincipal`).
- **A group administrator may run it only when no active platform operator exists** (`apps/recovery/services.py`, `_require_operator`; `apps.access.policy.active_platform_operator_exists`). A disaster needs a way out on an installation that never had a platform operator.
- **Q23's suspension covers every privileged account.** Without a failed database, every active account that `apps.access.policy.privileged_accounts` returns is suspended until a human confirms it (`manage confirm_account`), except the operator who runs the reconciliation. That set is staff and superuser accounts plus every account with a valid `GROUP_ADMIN`, `MATERIAL_MANAGER` or `ENGINEER` membership (`PRIVILEGED_ROLES`, ADR-0014).
- Choosing who runs the command is an operations rule. `is_superuser` still authorizes no business action (ADR-0002, ADR-0011).

## Consequences

- The runbooks (`docs/runbooks/backup-restore.md`, `deploy-rollback.md`) name a platform operator as the person who restores. The group-administrator fallback exists only for an installation without one.
- A privileged role added later (as `ENGINEER` was) is suspended by a restore without a code change, because the set comes from `PRIVILEGED_ROLES`.
- Tests: `tests/unit/recovery/test_reconcile.py` (a group administrator is refused while an operator exists and allowed when none does; material managers, engineers and operators suspended without a failed state) and the M8.4 rehearsal.
