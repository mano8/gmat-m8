# ADR-0008: Caddy and WhiteNoise; no Node.js at runtime

- **Status:** Accepted
- **Date:** 2026-10-26

## Context

The stack needs automatic HTTPS, bounded request bodies, filtered access logs and a maintenance gate on one VPS.

## Decision

Caddy terminates TLS (admin API off), compresses, bounds slow request bodies and headers, redacts token URLs from its logs and serves the maintenance gate. Static files are served by WhiteNoise from the app image. Tailwind is built by the standalone CLI in a build stage. Only Caddy publishes ports.

## Consequences

- Fewer components than a Traefik setup for the same result.
- No Node.js toolchain in the repository, the image or CI.
