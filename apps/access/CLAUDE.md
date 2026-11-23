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
