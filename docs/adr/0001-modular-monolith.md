# ADR-0001: Modular monolith: Django 6.1 + HTMX + PostgreSQL 18

- **Status:** Accepted
- **Date:** 2026-10-26

## Context

About 30 users on one inexpensive VPS. The app needs forms, sessions, CSRF, migrations, an admin and a mobile-first UI for technicians. Two runtimes or an API contract would add surface without a requirement that needs it.

## Decision

One Django project and one PostgreSQL database, with an app per domain under `apps/`. Business logic lives in `services.py`, reads in `selectors.py`, and views stay thin.

- **Backend:** Django 6.1; move to 6.2 LTS when it ships.
- **Frontend:** server-rendered templates with htmx 2.x, vendored and pinned, history cache disabled. No SPA, no Alpine.js (it needs `unsafe-eval` under CSP).
- **CSS:** Tailwind v4 standalone CLI in a Docker build stage. No Node.js in the repository or at runtime.
- **Database:** PostgreSQL 18 with psycopg 3 and `scram-sha-256`. CHECK constraints, composite foreign keys, row locks, advisory locks, exported snapshots, `pg_trgm` + `unaccent` and triggers are used on purpose.
- **Python:** 3.14 only; this is an application shipped as one image.
- **API:** none in the MVP. A later Django Ninja layer reuses the services.

## Consequences

- Few moving parts and the most built-in security features.
- A clear path to an inventory module (business phase 2).
- Every test runs on PostgreSQL; there is no SQLite layer.
- A change of stack (Redis, Celery, a JS framework, an API layer) needs an approved plan change.
