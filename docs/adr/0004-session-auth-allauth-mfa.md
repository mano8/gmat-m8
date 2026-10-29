# ADR-0004: Session authentication with django-allauth (>= 65.20.1) and MFA

- **Status:** Accepted
- **Date:** 2026-10-26

## Context

Sign-up is closed, accounts are created by invitation, and privileged roles need a second factor. A single-sign-on provider for one 30-user application would add Redis, JWT handling and another service.

## Decision

- Session authentication through django-allauth (account + MFA), floor **65.20.1**, enforced in CI.
- Email login, closed sign-up, enumeration prevention, POST-only logout. Allauth's email-management and email-confirmation views are off.
- TOTP and recovery codes, secrets encrypted at rest with `MFA_ENCRYPTION_KEYS`; passkeys are opt-in.
- Argon2id password hashing.
- The Django admin logs in through allauth and requires a session-level MFA check. Access and document models are read-only there; deletes are unavailable.
- Email addresses are changed by administrators only (Q26); the new address confirms and the old one can cancel for 24 h.
- No external identity provider in the MVP. allauth can act as an OIDC client later.

## Consequences

- Most of the authentication surface is maintained upstream, so the floor must follow security releases.
- `User.authz_generation` and `User.session_epoch` exist from the first migration so revocation and session invalidation can be serialized (ADR-0011).
