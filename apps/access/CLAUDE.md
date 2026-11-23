# `apps/access`

What this app holds and how its parts fit. Loaded when you work in this directory; the
rules that hold everywhere are in the root `CLAUDE.md`. Keep this file current when the
app changes.

## Layout

- `roles` (`Role`, `Capability`, `ROLE_CAPABILITIES`, `PRIVILEGED_ROLES`, `SELF_DECIDING_ROLES`)
- `context` (`AccessContext`, `scope_for`)
- `scopes` (`Scope`, `ScopedQuerySet.visible_to`, `InstallationQuerySet` for catalog and supplier master data)
- `principals` (`CONSTRUCTOR_MODULES`)
- `authorization` (`authorize_in_txn`, `authorize_self_decision`)
- `policy` (privilege, MFA, staff accounts)
- `middleware` (`request.access`)
- `mixins` (`require`, `@mutating`, `MutatingViewMixin`)
- `{% can %}`
- `AccessState`, L0 lock helpers (`generations`)

## Rules in detail

The full text of the root `CLAUDE.md`'s rules this directory implements; the root keeps a one-sentence summary of each.

- Roles: `GROUP_ADMIN`, `MATERIAL_MANAGER`, `ENGINEER` (privileged, branch-level; ranks below `GROUP_ADMIN` and above every other role: every material-manager capability; ADR-0014), `WAREHOUSE_OPERATOR`, `TECHNICIAN`, `SITE_MANAGER`, `PARTNER_REQUESTER`. Self-decisions: `SELF_DECIDING_ROLES` decide their own requests through a grant of that role on the request, with session MFA (`authorize_self_decision`, audit `self_decided: true`); other reviewers only where the procurement branch sets `allow_self_approval`.
- Scope-defining changes (company/branch `is_active`, `managed_by`, project branches/status) lock `AccessState` `FOR UPDATE`, and changes to a user's authority lock that `User` `FOR NO KEY UPDATE`, before `authorize_in_txn`; database triggers (`access.0002`, `accounts.0003`) bump `org_generation` / `authz_generation`. Memberships and assignments are revoked, never deleted (trigger).
