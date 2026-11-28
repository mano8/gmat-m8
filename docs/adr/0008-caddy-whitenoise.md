# ADR-0008: Caddy and WhiteNoise; no Node.js at runtime

- **Status:** Accepted
- **Date:** 2026-10-26
- **Governs:** `docker_compose/gmat_m8/caddy/`, `docker/app/Dockerfile`

## Context

The stack needs automatic HTTPS, bounded request bodies, filtered access logs and a maintenance gate on one VPS.

## Decision

Caddy terminates TLS (admin API off), compresses, bounds slow request bodies and headers, redacts token URLs from its logs and serves the maintenance gate. Static files are served by WhiteNoise from the app image. Tailwind is built by the standalone CLI in a build stage. Only Caddy publishes ports.

*Amended 2026-11-28 by [ADR-0034](0034-edge-modes-opt-in.md)* (M13):

- Outside the opt-in tunnel mode, Caddy believes no forwarded header, and the visitor is the connection's peer. In that mode (`EDGE_PROXY=cloudflare`), Caddy believes `CF-Connecting-IP`, only from the `cloudflared` service's one fixed address, and Cloudflare terminates the visitor's TLS before Caddy does (exception X16).
- Caddy writes the `X-Forwarded-For` that Django reads, in every mode.
- Caddy may read its certificate from files instead of ACME (`EDGE_TLS=files`).
- Caddy runs as uid 1000 with no capability (M13.2).

## Consequences

- Fewer components than a Traefik setup for the same result.
- No Node.js toolchain in the repository, the image or CI.
