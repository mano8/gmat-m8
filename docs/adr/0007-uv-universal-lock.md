# ADR-0007: uv with a universal, hash-verified lock

- **Status:** Accepted
- **Date:** 2026-10-26

## Context

Developers use Windows machines; production runs on amd64 and arm64 Linux. A platform-specific lock file needs regenerating per platform.

## Decision

Dependencies are declared in `pyproject.toml` (runtime, `dev` and `e2e` groups) and locked in one universal, hash-verified `uv.lock`. `uv sync` and `uv lock --check` are the only install paths. Security floors (allauth >= 65.20.1) are checked in CI against the lock.

## Consequences

- One lock works on every platform.
- Images and CI install from the lock only.
- Dependency updates arrive through grouped Dependabot PRs.
