# Changelog

All notable changes are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/). Release notes are taken from this file.

## [Unreleased]

### Added

- Django 6.1 project scaffold: `config/` settings (`base`, `dev`, `test`, `prod`, `build`) with typed, fail-closed environment helpers.
- `apps/core` with `/healthz/` (liveness) and `/readyz/` (database readiness) endpoints.
- `apps/accounts` with a custom `User` (email login, UUIDv7 key, `offboarded_at`, `authz_generation`, `session_epoch`, `privilege_elevated_at`).
- `scripts/check_access_rules.py`, a static guard for banned constructs and forbidden workflow triggers.
- Dependency management with uv (`pyproject.toml`, universal `uv.lock`) and tool configuration for ruff, mypy, pytest and coverage.
- Repository hygiene files, pre-commit configuration, agent deny rules, architecture decision records (ADR-0001 to ADR-0013) and a pull-request template.
- CI pipeline (`.github/workflows/ci.yaml`): `lint`, `typecheck`, `security`, `test`, `trivy-image` and `secret-scan`, with a PostgreSQL 18 service, coverage gates, migration reversibility, Trivy policy without `--ignore-unfixed` and `.trivyignore.yaml` for documented, expiring exceptions.
- Guards `scripts/check_security_floors.py`, `check_trivyignore.py`, `check_coverage_gates.py` and `check_migrations_reversible.py`, workflow policy tests and a monthly Dependabot configuration.
- Security baseline ([docs/security.md](docs/security.md)):
  - django-allauth with email login, closed sign-up, no email-management, email-confirmation or login-by-code views, POST-only logout, no account enumeration, and a password reset that never logs in and is redeemed under the user-row lock;
  - MFA (TOTP, recovery codes, optional passkeys) with secrets encrypted by `MFA_ENCRYPTION_KEYS`, a session-level MFA check that only counts the user's own authenticators, and pending logins discarded at each new login;
  - session epochs with a database trigger (password, email and deactivation end every session; counters never decrease) and session lifetimes of 12 hours for privileged accounts and 14 days for the others;
  - admin login through allauth with session MFA, `ServiceBackedModelAdmin` and read-only admins for accounts;
  - `SecurityDatabaseCache` for rate limits: atomic, never evicts live entries, JSON only, 429 when full;
  - CSP, `Permissions-Policy`, `__Host-` cookies, `no-store` for authenticated and token pages, `Clear-Site-Data` on logout, request ids;
  - JSON logging with token redaction on every handler, a redacting error-report filter and an error-tracker scrubber;
  - fail-closed system checks (`gmat.E001`–`gmat.E017`).
- Container images (M1.4):
  - application image `docker/app` (digest-pinned Python base, Tailwind and uv build stages, uid 1000, read-only root filesystem, `entrypoint.sh` with `web`, `migrate`, `check`, `migrate-plan`, `healthcheck` and `manage`, Gunicorn `gthread` with the access log off and the error log redacted) and a `dev` target;
  - backup image `docker/backup` (`backup-now`, `restore --verify`, snapshot-consistent dumps without session and cache data, manifest queries in the same snapshot, append-only restic upload, 6-hour schedule);
  - `docker/postgres/initdb/10-roles.sh` creating the owner, runtime and backup roles with `scram-sha-256` and refusing weak or placeholder passwords;
  - the real `trivy-image` job (both images, HIGH and CRITICAL, no `--ignore-unfixed`) and a non-required arm64 build job; Dependabot watches both Dockerfiles.
- Compose stack (M1.5) in `docker_compose/gmat_m8/`:
  - `docker-compose.yml` (Caddy, migrate, web, PostgreSQL 18, backup) with one internal database network per client, an internal `edge` network shared only by Caddy and `web`, hardened read-only containers and resource limits;
  - overlays for development (Mailpit, Tailwind watch, `runserver`), production (images by digest, ACME, ports 80/443) and the offline restore drill (every network internal);
  - Caddyfile with the admin API off, header and body read timeouts, the maintenance gate (`flags/maintenance.on`), the fallback page, and a filtered access log that redacts token URLs, query strings, `Referer` and `HX-Current-URL`;
  - `init.sh` generating env files and secrets (`--ci`, `--reset-db`), `.env.example`, `app.env.example`, `app.env.production.example`;
  - `DJANGO_ADMIN_URL_PATH` to move the admin, shared with Caddy's `ADMIN_URL_PATH`;
  - `tests/live` (18 smoke tests: headers, `check --deploy`, rate limits across workers, forged `X-Forwarded-For`, log sentinel, maintenance gate, stalled upload, network isolation, role privileges, backup then `restore --verify`) and the `compose-smoke.yaml` workflow (`scope` → `smoke` → `compose-smoke-gate`).
- UI shell (M1.6): mobile-first `base.html` with an app layout (top bar, sidebar from `md`, bottom navigation on phones) and an entrance layout; components (button, card, form field group, table that collapses to cards, pagination, empty state, toast messages, icons); Tailwind v4 theme tokens with dark mode; htmx 2.0.11 vendored with `historyCacheSize: 0`; restyled allauth pages and forms (the project's form templates); error pages 400/403/404/405/409/429/500; Spanish, English and French catalogs with a language switcher; hashed static files.
- Release and publish pipeline (M1.7):
  - `docker-publish.yaml`, run by a `v*` tag push (a manual dispatch builds without pushing): verifies that the commit is on `main` with green `ci.yaml` and `compose-smoke.yaml` runs on that exact SHA, builds both images for amd64 and arm64, gates each with Trivy, writes SBOMs, pushes by digest, gates the pushed digest again, signs with cosign (keyless), attests build provenance, signs `release-manifest.json` and creates the GitHub Release from these notes;
  - `image-scan.yaml`, a nightly Trivy scan (amd64 and arm64) of the released images, Caddy and PostgreSQL that opens, updates and closes one issue;
  - `scripts/verify_release.py` (the release gate, runnable on any commit without a tag) and `scripts/release_tools.py` (release notes, manifest, scan matrix, issue text), with unit and workflow-policy tests;
  - the release procedure, manifest format and manual verification commands in [docs/release.md](docs/release.md).
- Core building blocks (M2.1):
  - `BaseModel` (UUIDv7 key, creation and update times) and `DocumentSequence` with `next_number()`: gapless numbers per scope from one locked upsert, returned on rollback;
  - in-transaction domain events (`apps.core.events`): synchronous subscribers that commit or roll back with the publisher;
  - idempotent create commands (`apps.core.idempotency`): signed keys bound to actor and operation, valid 24 hours, `IdempotencyRecord` kept 7 days (system check `gmat.E006`), canonical payload hashes, replay re-authorization and 409 on a reused key;
  - the lock protocol (`apps.core.locking`): levels L0–L9 with L4b, an order-enforcing `LockOrder`, discover → lock → verify, the shared retry wrapper for deadlocks, lock timeouts and serialization failures, and named advisory locks;
  - composite foreign-key migration operations, quantity (`Decimal(12,3)`) and price (`Decimal(14,4)`) fields that refuse floats, the `--operator` management-command base and an admin base that leaves related objects to the service;
  - `apps/audit`: `AuditEvent` with `record()`, append-only through a database trigger.

### Changed

- Pre-authentication form pages keep `Referrer-Policy: same-origin`; only a URL that carries a token is `no-referrer` (browsers send `Origin: null` on a form POST under `no-referrer`, which CSRF refuses, so login failed in a real browser).
- Production settings now also require `DJANGO_CSRF_TRUSTED_ORIGINS` (https only) and `MFA_ENCRYPTION_KEYS`.
- Mail is configured through Django 6.1's `MAILERS`. Production uses a backend that sends nothing until the outbox arrives (M2.5).
