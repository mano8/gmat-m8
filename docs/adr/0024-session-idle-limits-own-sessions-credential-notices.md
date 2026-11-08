# ADR-0024: Idle limits, one's own sessions and credential-change notices

- **Status:** Accepted (the maintainer's decisions X5, X6 and Q3 of M8.3, and Q12 as amended on 2026-11-05; implemented in PR #33)
- **Date:** 2026-11-08 (recorded); merged 2026-11-07
- **Complements:** ADR-0004

## Context

ADR-0004 sets session authentication with django-allauth and MFA: absolute session lifetimes (Q12: 12 hours for privileged roles, 14 days for the others) and MFA for privileged roles. The M8.3 security review found three gaps:

- a session left open on a shared phone stayed valid for its whole lifetime;
- users could not see or end their other sessions;
- a changed password or second factor went unnoticed by its owner.

## Decision

- **Idle limits** on top of the absolute lifetimes: 30 minutes for privileged accounts and 2 hours for the others, from the environment (`DJANGO_PRIVILEGED_SESSION_IDLE_SECONDS`, `DJANGO_SESSION_IDLE_SECONDS`; `apps.accounts.sessions`). A request that a page sends by itself (a poll) does not count as activity (`@session_passive`), so an open tab never keeps an idle session alive.
- **One's own sessions.** Anyone signed in lists their own sessions and ends one or all of the others (`/account/sessions/`). The check is `OWN_ACCOUNT` in `authorize_in_txn`: no role's capability is involved, and a privileged account needs session MFA.
- **Credential-change notices.** A password or second factor that changes tells its owner by email (`ACCOUNT_EMAIL_NOTIFICATIONS`), through the outbox like every other message (ADR-0009), and is audited (`user.password_changed`, `user.authenticator_removed`).

## Consequences

- A privileged user is signed out after 30 minutes without activity. Every new polled fragment must be `@session_passive`.
- A user who suspects a stolen session can end it without an administrator.
- The two audit actions are credential events: `apps.recovery.replay` replays them after a restore (ADR-0016).
- `docs/security.md` keeps the baseline and the register entries (X5, X6).
