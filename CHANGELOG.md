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
- CI pipeline (`.github/workflows/ci.yaml`): `lint`, `typecheck`, `security`, `test`, `trivy-image` (placeholder until M1.4) and `secret-scan`, with a PostgreSQL 18 service, coverage gates, migration reversibility, Trivy policy without `--ignore-unfixed` and an empty `.trivyignore.yaml`.
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

### Changed

- Production settings now also require `DJANGO_CSRF_TRUSTED_ORIGINS` (https only) and `MFA_ENCRYPTION_KEYS`.
- Mail is configured through Django 6.1's `MAILERS`. Production uses a backend that sends nothing until the outbox arrives (M2.5).
