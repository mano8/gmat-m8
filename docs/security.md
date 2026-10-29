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
| `Cache-Control: no-store` | login, logout, reauthentication, all password-reset steps, MFA authenticate and reauthenticate, anonymous visitors included (invitation and email-change pages join in M2.6) |
| `Referrer-Policy: no-referrer` | only a URL that carries a token (`/accounts/password/reset/key/<token>/`, which redirects to a key-less URL). Pages with a form keep `same-origin`: under `no-referrer` browsers send `Origin: null` on a form POST and Django's CSRF check refuses it (found in a real browser in M1.6) |
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

## Images, database roles and backups (step M1.4)

Sources: `docker/app/`, `docker/backup/`, `docker/postgres/initdb/`. CI builds both images and holds them to the Trivy gate (`trivy-image`).

- **Application image** (`docker/app/Dockerfile`): `python:3.14-slim` pinned by digest, stages `assets` (Tailwind standalone CLI, sha256-verified) → `builder` (`uv sync --frozen --no-dev`, hashes from `uv.lock`) → `runtime`, plus a `dev` target. Runs as uid/gid 1000 on a read-only root filesystem; source is owned by root; pip is removed; `collectstatic` runs with `config.settings.build` (no database, no secret). `entrypoint.sh` is the only entry point (`web`, `migrate`, `check`, `migrate-plan`, `healthcheck`, `manage`; `worker` arrives with M2.5, `verify-internal` and `post-restore-reconcile` with M8.4).
- **Gunicorn** (`gunicorn.conf.py`): `gthread`, access log off (Caddy is the access log), no control socket, and `FORWARDED_ALLOW_IPS` must list the edge subnet: it refuses to start with a wildcard or an empty value. Its error log uses the same JSON formatter and redaction filter as Django.
- **Database roles** (`10-roles.sh`, run once at database initialisation, idempotent): `owner` (owns the schema; used only by the one-shot `migrate`), `runtime` (SELECT/INSERT/UPDATE/DELETE only; owns nothing, so it cannot alter or drop tables, triggers or the audit table) and `backup` (`pg_read_all_data`, read-only sessions). All use `scram-sha-256`. The script refuses placeholder or short passwords, repeated names or passwords, and a role password equal to the bootstrap superuser's.
- **Backups** (`docker/backup/`): every 6 hours and on demand (`backup-now [--label]`). A psql session takes the advisory lock `gmat:media-backup` (key `hashtextextended('gmat:media-backup', 0)`; the worker's media purge uses the same key with `pg_try_advisory_lock`), opens a `REPEATABLE READ READ ONLY` transaction and exports its snapshot. `pg_dump -Fc --snapshot` leaves out the data of `django_session` and `django_cache`, and the manifest queries (row counts of every table, migration head, newest audit event) run in the same snapshot, so dump and manifest agree under concurrent writes. The media directory, when there is one, is copied under the lock and checksummed. Files are mode 0600 in a 0700 directory and kept 3 days locally. The offsite upload uses restic with a fixed host and tags and **never prunes**; `forget`/`prune` run only from the maintainer's machine (plan §9).
- **`restore --verify`** (weekly, and on demand) restores the newest dump into a private PostgreSQL started inside the backup container on a unix socket (no network, no write right on the real database), then compares every row count, the migration head and the newest audit event with the manifest. Tables whose data is excluded must come back empty.
- **Exceptions** in `.trivyignore.yaml` are Debian base-image packages with no fixed version yet; each has a statement, an owner and an expiry at most 90 days away, and needs the maintainer's explicit approval in the PR.

## The compose stack (step M1.5)

Files in `docker_compose/gmat_m8/` (see its README); `tests/live` proves the points below against a running stack and the `compose-smoke` workflow runs it.

- **Networks.** One internal network per database client (`db_web`, `db_worker`, `db_backup`, `db_migrate`), so `web`, `migrate`, `backup` and the worker share none and cannot reach each other. `edge` is internal and shared only by Caddy and `web` (no route out for `web`); Caddy alone sits on `ingress`, the one network that publishes ports. The database is never published (the development overlay publishes it on 127.0.0.1 only).
- **Hardening.** `cap_drop: ALL`, `no-new-privileges`, read-only root filesystem, memory and process limits and log rotation on every service; `web`, `migrate` and `backup` run as uid 1000. Caddy runs as root inside its container with only `NET_BIND_SERVICE` (its data volumes are root-owned; nothing else is writable).
- **Caddy.** Admin API off; `read_header` 10 s and `read_body` (`CADDY_READ_BODY_TIMEOUT`, 120 s) so a client that stops sending frees its Gunicorn thread; body limit `MAX_UPLOAD_SIZE`; no trusted proxies, so a client-sent `X-Forwarded-For` never reaches Django; `Server` header removed; HTTP/3 only where the UDP port is published. The admin prefix (`ADMIN_URL_PATH`, the same value in Django) is limited to `ADMIN_ALLOWED_CIDRS`.
- **Maintenance gate.** `flags/maintenance.on` makes Caddy answer 503 with the maintenance page for every public request while `web` keeps running; the same page replaces a missing `web` (502/503/504).
- **Log redaction.** Caddy's default and access logs replace the token paths (`/accounts/password/reset/key/…`, `/account/email-change/…`, `/invite/…`) and every query string with `REDACTED` and drop `Referer` and `HX-Current-URL`. Only paths under those prefixes are secrets to Caddy; Django's own filter also redacts any long random segment.
- **Secrets.** `init.sh` generates every secret (mode 0600, never printed, redrawn if it resembles a placeholder). A secret is defined once in `.env` and reaches the services by interpolation; Django-only settings are in `app.env`. Only `*.example` templates are committed.
- **Not yet.** The worker service and its heartbeat (M2.5), `deploy.sh` and `recover.sh` (M8.4), which will verify the signed release manifest and image digests that the publish pipeline (M1.7, [release.md](release.md)) produces before writing them into the production overlay.

## The UI shell (step M1.6)

Templates in `templates/`, styles in `static/src/app.css`, code in `static/js/app.js` and `static/vendor/`.

- **No inline code.** Pages carry no inline script, style or event handler (`tests/unit/core/test_ui_shell.py` scans the rendered login, home and error pages; a real-browser run against the stack showed zero console and CSP errors). The only `<script>` blocks are allauth's `application/json` data blocks, which the CSP does not execute.
- **htmx** 2.0.11 is vendored (`static/vendor/README.md` records its source and checksum; a test pins the sha256). It is configured by `<meta name="htmx-config">`: `historyCacheSize: 0`, `refreshOnHistoryMiss`, no eval, no script tags, no indicator styles, `selfRequestsOnly`. The CSRF token travels in `hx-headers` on `<body>`; the cookie is `HttpOnly`. Nothing is written to `localStorage` (checked in the browser).
- **Static files** are hashed (`CompressedManifestStaticFilesStorage`) and served by WhiteNoise with a far-future cache header; the Tailwind input is not shipped.
- **Language**: Spanish by default, English and French; the choice is a `__Host-language` cookie (`Secure`, `HttpOnly`, `SameSite=Lax`) set by a POST form. The form's `next` is the current path, except on a token URL, where it is `/` so a secret is never copied into another form. Catalogs are in `locale/`; the `.mo` files are compiled in the image build.
- **Error pages** 400, 403 (including the CSRF failure), 404, 405, 409, 429 and 500 show a code and a generic sentence, never a path, a query, an exception or a user. 405, 409 and 429 are produced by `ErrorPagesMiddleware` for bare responses; 500 renders without a request.
- **Referrer policy.** `no-referrer` only on a URL that carries a token. A form page keeps `same-origin`: under `no-referrer` browsers send `Origin: null` on its POST and Django's CSRF check refuses it, so login would fail. (Found by logging in with a real browser; the M1.3 tests asserted the header, not the login.)

## Still to come

- M2.3: the access engine (`authorize_in_txn`, principals, privileged roles for MFA and session lifetimes, `GROUP_ADMIN` in the admin).
- M2.5: outbox, worker, and the cache housekeeping (`SecurityDatabaseCache.purge_expired`).
- M2.6: invitations, administrator email changes, offboarding, `bootstrap_admin`; their token pages join the no-referrer list and the log redaction prefixes.
- M8.2: the same checks in a real browser (e2e).
