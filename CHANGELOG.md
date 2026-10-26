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
