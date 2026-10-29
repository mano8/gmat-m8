# ADR-0002: Shared-schema multi-company with a code-defined scope policy

- **Status:** Accepted
- **Date:** 2026-10-26

## Context

A group of companies and branches shares one deployment. Django permissions are global, and Django Groups cannot express "approver of branch X only". Users can hold several memberships with different roles.

## Decision

One shared schema. One access-policy module (`apps/access`) scopes every query:

- a code-defined `ROLE_CAPABILITIES` matrix plus scoped, time-bounded memberships;
- scoped models are reached only through `visible_to(ctx, capability)` or selectors;
- capabilities are never mixed across memberships (`ctx.scope_for(capability)`);
- out of scope answers 404, in scope without the capability answers 403;
- `is_superuser` marks platform operators and never authorizes a business action.

Schema-per-tenant is rejected, and PostgreSQL row-level security is deferred.

## Consequences

- One set of migrations and one connection pool.
- Isolation depends on discipline, so CI enforces it: `scripts/check_access_rules.py`, the access acceptance matrix and per-projection leak assertions.
- Row-level security can be added later as a second layer without changing the model.
