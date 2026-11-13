# ADR-0030: Administration happens in the app; the Django admin is optional and read-only

- **Status:** Proposed (the maintainer's decisions of 2026-11-12, Q70–Q72, issue #53; accepted when the pull request of that phase merges)
- **Date:** 2026-11-13
- **Governs:** `apps/core/admin.py`, `apps/accounts/admin_site.py`, `apps/*/admin.py`, `config/urls.py`, `apps/accounts/management/commands/reset_mfa.py`, `apps/catalog/services.py`, `docs/design/admin-inventory.md`

## Context

The Django admin was the master-data screen. Group administrators edited companies, joint-venture members, branches, warehouses and worksites (`org.manage`), categories and materials (`catalog.manage`) and suppliers (`supplier.manage`) there. The admin saved through the same services as the app (`ServiceBackedModelAdmin`), so generations, audit and deactivation rules applied. Everything else was read-only there, and nothing could be deleted. In production, Caddy lets only the addresses in `ADMIN_ALLOWED_CIDRS` reach it, and the admin site admits only platform operators and group administrators with session MFA.

That had three costs:

- A group administrator away from the allowlisted addresses could not open a branch or a warehouse.
- The admin is outside the app's gates: the design system, the phone layout, dialogs, accessibility and keyboard tests, translations and the access acceptance matrix.
- An editable admin is privileged surface that the checks have to police: `gmat.E005`, `lookup_allowed`, the refusal of inline saves.

Some work had no screen at all. Units of measure were seeded and read-only everywhere. A lost second factor needed `manage reset_mfa` on the server. A person's full name, required since ADR-0029, could not be corrected anywhere.

## Decision

1. **Everything a person administers has a screen in the app.** [`docs/design/admin-inventory.md`](../design/admin-inventory.md) classifies every model registered in the admin and every `manage` command, and maps the screens that are added: companies and joint-venture members, branches (incl. the managing branch, self-approval, the default warehouse and activity), warehouses, worksites' names and activity, units of measure, a person's name and the second-factor reset. The screens call the services that exist. A rule a screen needs that no service has is added to the service, never to the view. Deactivating or reactivating a company or branch, and changing a branch's managing branch, open a danger dialog that previews whose access changes. The service decides, and its refusal is shown in the dialog.
2. **The admin is optional.** `DJANGO_ADMIN_ENABLED` turns it on; it is off by default in production and on in development and tests. While it is off, no admin URL exists and the prefix answers 404 like any unknown path. The admin app stays installed, so its system checks still run. While it is on, admission is unchanged (platform operators and group administrators, allauth login, session MFA, `lookup_allowed`, no deletes), and the deploy requires `ADMIN_ALLOWED_CIDRS` and refuses an open list. Without the admin, the deploy needs no allowlist.
3. **The admin is read-only for every model.** No `ModelAdmin` can add or change a record, whoever is signed in. Three checks make sure a future model cannot become editable again: a system check next to `gmat.E005`, a rule in `scripts/check_access_rules.py`, and a test that every registered model has a row in the inventory.
4. **A lost second factor is reset from the person's page** by an `org.manage` holder with session MFA, through `accounts.services.credentials.reset_user_mfa` as a session operation. A reason is required, and so is a tick that the person's identity was checked outside the application (stored in the audit payload). The effects are the command's: every factor and the recovery codes removed, every session ended, the owner told by email through the outbox (ADR-0024), audited as `user.mfa_reset` naming the administrator, and logged to `gmat.security`. Nobody resets their own factors (ADR-0022). A staff or superuser account is reset only by `manage reset_mfa` run by a superuser, which stays as the break-glass path.
5. **Group administrators manage units of measure** (`org.manage`). A unit's `code` is unique and fixed once any material uses it. Its `name` is always editable. Its `decimals` may be raised at any time and lowered only while no material uses it. A unit is never deleted. A new `is_active` flag (expand migration, default true) withdraws a unit from the choices for new materials and from imports, while existing materials keep it. The seeded units stay.
6. **A group administrator corrects a person's full name** on the person's page (ADR-0029, point 5), staff and superuser accounts excepted. A name is neither authority nor an address, so one's own name is allowed too.
7. **Operator work stays on the command line**, for the maintainer on the server: `bootstrap_admin` (before any account exists), `post_restore_reconcile`, `confirm_account` and `outbox_quarantine` (recovery), `reencrypt_mfa` (key rotation), `reset_mfa` (break-glass), the bulk imports (for now; an upload screen is a separate decision), and the deploy and recovery scripts. Django's `changepassword`, which writes no audit event that a restore could replay, is disabled like `createsuperuser`.

## Alternatives rejected

- **Keep the admin as the master-data screen and widen the allowlist.** The work would still be outside the app's gates, and the allowlist exists because the admin is a privileged surface. Widening it undoes what the allowlist protects.
- **Remove the admin altogether.** Being able to inspect any record read-only helps the maintainer, and keeping it on in development and tests keeps those read-only pages tested. Off by default in production costs nothing.
- **Leave the admin editable for platform operators only.** Platform operators hold no business authority; nothing is authorized with `is_superuser`.

## Consequences

- [ADR-0004](0004-session-auth-allauth-mfa.md)'s admin bullet now says this. Plan §5 "Django Admin" carries the same amendment.
- The deploy rules of [ADR-0012](0012-maintenance-gate-deploys-recovery.md) and [ADR-0017](0017-signed-release-facts-and-public-image-packages.md) (`deploy.sh`, `ops/checks.py`) change to require the allowlist only while the admin is on. Those ADRs are updated with that change, and `docs/security.md`'s admin section and exception X10 are rewritten.
- Group administrators no longer edit master data in the admin, even when it is on: catalog and supplier screens already exist, and the rest arrive with these screens.
- Every new screen gets its rows in the access acceptance matrix, and its pages pass the browser, accessibility and keyboard tests like any other.
- The `Governs` line grows as the screens and checks land: the new views and services, `apps/core/checks.py` for the check, the settings switch.
