# ADR-0022: An administrator never changes their own authority or email

- **Status:** Accepted (the maintainer's decision of 2026-11-07; implemented in PR #38)
- **Date:** 2026-11-08 (recorded); merged 2026-11-07

## Context

Group administrators manage people through `org.manage`: memberships, validity, invitations, email changes, offboarding and the MFA reset. Their own person page showed the same tools. An administrator could revoke their own last `GROUP_ADMIN` membership and lock everyone out, or change their own email address, so that a stolen session could redirect password resets (F59).

## Decision

On their own account an administrator changes **only their employer** (`set_employer_company`). The employer matters only for partner memberships, and `org.manage` belongs to group administrators alone. The services refuse the actor's own account with code `self`, whatever the page shows:

- granting, revoking or changing the validity of one's own memberships (`apps/organizations/services.py`, `_not_own_roles`);
- an email change of one's own address (`apps/accounts/services/email_changes.py`);
- offboarding oneself (`apps/accounts/services/offboarding.py`);
- resetting one's own second factors (`apps/accounts/services/credentials.py`).

Another administrator does these. On one's own person page only the employer is offered, with a note that roles and email are changed by another administrator, but the services are the guard: a direct POST is refused.

## Consequences

- No one raises or lowers their own authority. A future capability (inventory, prices, budgets) is reached only through a membership that another administrator grants.
- An installation with a single administrator cannot change that administrator's roles or email until a second one exists.
- `docs/security.md` ("One's own account") describes the same rule as part of the security baseline.
- Tests: `tests/unit/organizations/test_services.py`, `tests/unit/organizations/test_views.py`, `tests/unit/accounts/test_email_changes.py`.
