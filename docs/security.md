# Security baseline

What the application enforces today (step M1.3), where it lives, and what later steps add. Decisions behind it: [ADR-0004](adr/0004-session-auth-allauth-mfa.md), [ADR-0009](adr/0009-no-redis-database-cache-outbox.md), [ADR-0011](adr/0011-authorize-in-transaction-principals.md).

## Authentication (`config/settings/base.py`, `apps/accounts/`)

- django-allauth (floor 65.20.1, checked in CI and by system check `gmat.E001`) is the only authentication backend. Login is by email and password, then MFA when the account has a factor.
- **Closed surface.** Sign-up, allauth's email management and email confirmation, login by code and "set first password" have no URL (`apps/accounts/urls.py`). Accounts arrive through invitations and email addresses change through the administrator flow (both M2.6).
- **No enumeration.** A wrong password and an unknown email give the same answer; a reset request for an unknown or inactive address sends nothing and looks the same.
- **Logout is POST-only** and answers with `Clear-Site-Data: "cache", "storage"`.
- **Password reset**: one-hour token, never logs anyone in, and is redeemed under a `SELECT … FOR UPDATE` of the user row with the token checked again (`AccountAdapter.redeem_password_reset`), so two concurrent redemptions cannot both succeed. Inactive accounts are refused.
- **Clean slate per login.** A new anonymous login discards any half-finished login in the session, and the login signal keeps only the authentication records of the account that logged in.
- Passwords: Argon2id, minimum length 12.

## Sessions (`apps/accounts/sessions.py`, `middleware.py`)

- Each session stores the user's `session_epoch` and its login time. `SessionGuardMiddleware` ends a session whose epoch is stale or whose lifetime is over: 12 hours for privileged accounts (staff and superusers today, privileged roles from M2.3), 14 days for the others.
- A database trigger (`accounts.0002`) bumps `session_epoch` on a password change, an email change and a deactivation, and keeps `session_epoch` and `authz_generation` from ever decreasing. `authz_generation` is bumped when `is_active`, `is_staff` or `is_superuser` changes.
- A password change and an MFA reset (`apps.accounts.mfa.reset_mfa`) end every session of the user; removing one's own factor ends the user's other sessions.
- Cookies: `__Host-sessionid` and `__Host-csrftoken`, `Secure`, `SameSite=Lax`, both `HttpOnly` (htmx reads the CSRF token from the page). `dev` uses plain names over HTTP.

## MFA

- TOTP and recovery codes; passkeys (WebAuthn) as an optional second factor, never as a password replacement. "Trust this browser" is off and TOTP accepts only the current 30-second step, which allauth then marks as used.
- **Session-level check** (`apps.accounts.mfa.session_passed_mfa`): the *current session* must have completed MFA after the user's last privilege elevation, with an authenticator that belongs to the logged-in user. An enrolled but unused factor does not count.
- **Secrets at rest**: `MFAAdapter` encrypts TOTP secrets and recovery-code seeds with `MultiFernet` over `MFA_ENCRYPTION_KEYS`. allauth's default adapter stores them in plaintext (a test proves it).
- **Key rotation**: put a new key first in `MFA_ENCRYPTION_KEYS` and keep the old ones; new secrets use the new key and old ones still decrypt. Re-encrypting existing secrets and dropping old keys is an operator procedure (runbook, M8.5). Keep an offline copy of the keys; losing all of them means every user re-enrolls TOTP.
- Privileged accounts cannot remove their last second factor.

## Admin (`apps/accounts/admin_site.py`, `apps/core/admin.py`)

- Logging in to the admin goes through allauth (`secure_admin_login`); Django's admin login form is never processed. Admin URLs need an active staff account and a session that completed MFA: a GET is sent to MFA reauthentication (or enrollment), anything else gets 403. `GROUP_ADMIN` access arrives with M2.3.
- Every `ModelAdmin` is built on `ServiceBackedModelAdmin` (system check `gmat.E005`): saves go through a service, deletes and the bulk delete action do not exist. Users, email addresses and authenticators are read-only, and MFA secrets and password hashes are never displayed. Django's groups are not registered.

## Rate limits and the security cache (`apps/core/cache.py`)

- allauth's rate limits (`ACCOUNT_RATE_LIMITS`) use the default cache, `SecurityDatabaseCache` over the migrated `django_cache` table: atomic `add` and `incr` under row locks, no eviction of live entries, JSON values (no pickle), bounded lock waits, and a capacity of `SECURITY_CACHE_MAX_ENTRIES`. At capacity, or when a lock wait times out, the write fails and the request gets **429**: authentication fails closed. The lock companion of a live counter is always admitted, so existing counters keep working.
- The client IP comes from `apps.core.http.client_ip`: with `ALLAUTH_TRUSTED_PROXY_COUNT = 1` only the address Caddy appends to `X-Forwarded-For` counts; forged entries further left are ignored.

## Headers (`config/settings/base.py`, `apps/core/middleware.py`)

| Header | Where |
| --- | --- |
| `Content-Security-Policy` (`default-src 'self'`, no inline script or style, `frame-ancestors 'none'`, `object-src 'none'`) | every response |
| `Permissions-Policy: camera=(), microphone=(), geolocation=(), payment=()` | every response |
| `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Cross-Origin-Opener-Policy: same-origin`, `Referrer-Policy: same-origin` | every response |
| `Cache-Control: no-store, private` and `Vary: Cookie` | every response to an authenticated user |
| `Referrer-Policy: no-referrer` and `no-store` | login, logout, reauthentication, all password-reset steps, MFA authenticate and reauthenticate, anonymous visitors included (invitation and email-change pages join in M2.6) |
| HSTS (one year), HTTPS redirect, `SECURE_PROXY_SSL_HEADER` | production settings |
| `X-Request-ID` | every response; a well-formed id from the proxy is kept |

## Logging (`config/logging.py`)

- One JSON handler on stdout. Every handler carries `RedactionFilter` (system check `gmat.E004`): token paths (the password-reset key, and any long random path segment) become `REDACTED`, query strings and fragments are dropped, `secret=value` pairs are masked, and the `request` attribute keeps only the method and the redacted path. `Referer` and `HX-Current-URL` are never logged.
- No `AdminEmailHandler` reaches Django's loggers. Error reports hide authentication POST bodies and URL-carrying headers (`RedactingExceptionReporterFilter`). `scrub_event` is the `before_send` hook for an optional error tracker (use it with `send_default_pii=False`).
- The sentinel test plants a secret in a reset URL, an invitation URL, an email-change URL, a password field and the `Referer` and `HX-Current-URL` headers, on 404, 500 and normal responses, and checks every log line.

## Email

No mail is sent from a request. Until the outbox and worker exist (M2.5), production uses `apps.core.mail.DisabledEmailBackend`, which drops messages and logs only how many: a reset link is never printed into container logs. Production is not live before M2.5. `dev` prints mail to the console, tests keep it in memory.

## System checks (`apps/core/checks.py`)

| Id | Refuses |
| --- | --- |
| `gmat.E001` | allauth below the floor |
| `gmat.E002` | weakened allauth or MFA settings (enumeration, logout on GET, login on reset, login by code, unknown-account emails, trusted browsers, passkey login, TOTP tolerance or bypass code, proxy count) or an open sign-up |
| `gmat.E003` | a default cache other than `SecurityDatabaseCache` |
| `gmat.E004` | a logging handler without redaction, or an email handler on Django's loggers |
| `gmat.E005` | a `ModelAdmin` not built on `ServiceBackedModelAdmin` |
| `gmat.E010`–`E017` (deploy) | `DEBUG`, a weak or placeholder `SECRET_KEY`, missing or development `MFA_ENCRYPTION_KEYS`, empty or wildcard `ALLOWED_HOSTS`, non-https `CSRF_TRUSTED_ORIGINS`, cookies that are not `Secure __Host-`, a first hasher other than Argon2, a production mailer that could send from a request |

`manage.py check --deploy` passes with zero warnings under production settings. M2.5 adds `OUTBOX_ENCRYPTION_KEYS` and makes the outbox the only production mail backend.

## Still to come

- M2.3: the access engine (`authorize_in_txn`, principals, privileged roles for MFA and session lifetimes, `GROUP_ADMIN` in the admin).
- M2.5: outbox, worker, and the cache housekeeping (`SecurityDatabaseCache.purge_expired`).
- M2.6: invitations, administrator email changes, offboarding, `bootstrap_admin`; their token pages join the no-referrer list and the log redaction prefixes.
- M1.5 and M8.2: the same checks through Caddy (compose smoke) and a real browser (e2e).
