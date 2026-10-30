# Security baseline

What the application enforces today (step M1.3), where it lives, and what later steps add. Decisions behind it: [ADR-0004](adr/0004-session-auth-allauth-mfa.md), [ADR-0009](adr/0009-no-redis-database-cache-outbox.md), [ADR-0011](adr/0011-authorize-in-transaction-principals.md).

## Authentication (`config/settings/base.py`, `apps/accounts/`)

- django-allauth (floor 65.20.1, checked in CI and by system check `gmat.E001`) is the only authentication backend. Login is by email and password, then MFA when the account has a factor.
- **Closed surface.** Sign-up, allauth's email management and email confirmation, login by code and "set first password" have no URL (`apps/accounts/urls.py`). Accounts arrive through invitations and email addresses change through the administrator flow (both M2.6).
- **No enumeration.** A wrong password and an unknown email give the same answer; a reset request for an unknown or inactive address sends nothing and looks the same.
- **Logout is POST-only** and answers with `Clear-Site-Data: "cache", "storage"`.
- **Password reset**: one-hour token, never logs anyone in, and is redeemed as a password-reset `TokenPrincipal` (`AccountAdapter.redeem_password_reset`): `authorize_in_txn` locks the user row and the token is checked again under that lock, so two concurrent redemptions cannot both succeed. Inactive accounts are refused.
- **Clean slate per login.** A new anonymous login discards any half-finished login in the session, and the login signal keeps only the authentication records of the account that logged in.
- Passwords: Argon2id, minimum length 12.

## Sessions (`apps/accounts/sessions.py`, `middleware.py`)

- Each session stores the user's `session_epoch` and its login time. `SessionGuardMiddleware` ends a session whose epoch is stale or whose lifetime is over: 12 hours for privileged accounts (platform operators and holders of a valid `GROUP_ADMIN` or `MATERIAL_MANAGER` membership, `apps.access.policy.is_privileged`), 14 days for the others.
- A database trigger (`accounts.0002`) bumps `session_epoch` on a password change, an email change and a deactivation, and keeps `session_epoch` and `authz_generation` from ever decreasing. `authz_generation` is bumped when `is_active`, `is_staff`, `is_superuser` or `employer_company` changes, and (`access.0002`) when a membership or project assignment of the user is created or changed.
- A password change and an MFA reset (`apps.accounts.mfa.reset_mfa`) end every session of the user; removing one's own factor ends the user's other sessions.
- Cookies: `__Host-sessionid` and `__Host-csrftoken`, `Secure`, `SameSite=Lax`, both `HttpOnly` (htmx reads the CSRF token from the page). `dev` uses plain names over HTTP.

## MFA

- TOTP and recovery codes; passkeys (WebAuthn) as an optional second factor, never as a password replacement. "Trust this browser" is off and TOTP accepts only the current 30-second step, which allauth then marks as used.
- **Session-level check** (`apps.accounts.mfa.session_passed_mfa`): the *current session* must have completed MFA after the user's last privilege elevation, with an authenticator that belongs to the logged-in user. An enrolled but unused factor does not count.
- **Role-based enforcement** (`apps.accounts.mfa.mfa_required_for`, `MFARequiredMiddleware`): for a privileged user every URL needs that check, except allauth's account and MFA pages (to complete MFA or sign out), the language switch and the health checks. A GET is sent to MFA reauthentication (or enrollment, without a factor); anything else gets 403. Inside every write, `authorize_in_txn` checks it again whenever only a privileged grant covers the operation.
- **Step-up.** Granting a privileged role sets `privilege_elevated_at` to the grant's database time (a role that starts later included), so every existing session of that user steps up before using any privileged grant.
- **Secrets at rest**: `MFAAdapter` encrypts TOTP secrets and recovery-code seeds with `MultiFernet` over `MFA_ENCRYPTION_KEYS`. allauth's default adapter stores them in plaintext (a test proves it).
- **Key rotation**: put a new key first in `MFA_ENCRYPTION_KEYS` and keep the old ones; new secrets use the new key and old ones still decrypt. Re-encrypting existing secrets and dropping old keys is an operator procedure (runbook, M8.5). Keep an offline copy of the keys; losing all of them means every user re-enrolls TOTP.
- Privileged accounts cannot remove their last second factor.

## Admin (`apps/accounts/admin_site.py`, `apps/core/admin.py`)

- Logging in to the admin goes through allauth (`secure_admin_login` sends anonymous visitors to its login page); Django's admin login form is never processed. Admin URLs need an active superuser or a holder of `org.manage` (a group administrator) as the access engine resolves it (`apps.access.authorization.may_use_admin`), and a session that completed MFA after the last elevation: a GET is sent to MFA reauthentication (or enrollment), anything else gets 403.
- Whoever the site admits may view every registered model. Only master data (companies, joint-venture members, branches, locations) can be added or changed, and only by a group administrator (`org.manage`, through `apps.access.admin.CapabilityModelAdmin`, per object for a scoped model); a platform operator without that membership only views. `is_superuser` grants no business capability. Users, memberships, projects, assignments and the access state are read-only, so a group administrator cannot edit a staff account or its flags; the services also refuse to change a staff account's memberships, assignments or employer unless the actor is a superuser.
- Every `ModelAdmin` is built on `ServiceBackedModelAdmin` (system check `gmat.E005`): saves go through a service, deletes and the bulk delete action do not exist. Users, email addresses and authenticators are read-only, and MFA secrets and password hashes are never displayed. Django's groups are not registered.
- The admin's password-change pages redirect to allauth's, so a password changes in one place, under its rate limit (M2.4). Admin logout hands over to allauth's logout the same way.

## Authorization (`apps/access/`, step M2.3)

- **Capability matrix as data** (`roles.py`, plan §3.4): `ROLE_CAPABILITIES` maps each role to its capabilities with a reach: `SCOPE` (●, everything in the membership's scope), `ASSIGNED` (○, the projects assigned to a site manager or partner) or `OWN` (○ own, the user's own documents). A test compares it with the plan's table. Catalog and supplier capabilities reach the installation-wide master data only.
- **Scopes** (`context.py`, `scopes.py`, plan §3.5): `AccessContext.scope_for(capability)` returns one grant per valid membership and never merges them (S6). S1: a branch and the active branches it manages. S2: everything for group administrators, inactive history included. S3: projects of a branch scope, by branch or procurement branch. S4: assigned projects (partners and technicians see active projects only). S5: documents by branch or project, own documents only for `OWN`. S8: half-open validity at the database clock, for an active, not offboarded user. S9: inactive companies and branches grant nothing and drop out of every scope; a partner membership grants nothing while its company is inactive, nor do the assignments that depend on it. A hypothesis test checks the resolution against a reference implementation on random organization graphs.
- **Reads**: `request.access` (`AccessContextMiddleware`) is built per request and reads nothing until asked; `Model.objects.visible_to(ctx, capability)` exists for `Branch`, `Project` and `Location`, and document models add theirs.
- **Views**: `require(request, capability, obj, read=..., methods=...)` answers 405 for another method, 404 for an object the user cannot see and 403 for one they see but may not act on (S7). `@require_capability` and `CapabilityRequiredMixin` add the login and coarse check; `@mutating` and `MutatingViewMixin` also accept POST only. `{% can %}` only hides UI.
- **Writes** (`authorization.py`, plan §3.6, S10): every mutating service calls `authorize_in_txn(principal, capability, *objects)` inside its transaction, in full every time. It locks `AccessState` and the actor's `User` row `FOR SHARE`, requires an active user whose session epoch is current (`StaleSession` otherwise, and the session is flushed), reloads the memberships at the transaction's clock, requires every object in the capability's scope (with `read`, an invisible object is a 404), and requires session MFA after the last elevation when only privileged grants cover the operation. Revocations take the same rows `FOR UPDATE` or `FOR NO KEY UPDATE`, so they wait for the writes in flight and apply to every later write.
- **Principals** (`principals.py`): `SessionPrincipal` (the session's epoch and MFA time), `TokenPrincipal` (one operation; the target and initiator rows are locked in id order before the token's own check, and the initiator must still hold `org.manage`, a superuser for a staff target), `SystemPrincipal` (`outbox.send`, `housekeeping.*`, no business capability) and `OperatorPrincipal` (the command's one capability, `via=cli:<command>`, no session MFA). The last three raise `PrincipalNotAllowed` when constructed outside `CONSTRUCTOR_MODULES` (`apps.accounts.tokens`, `apps.accounts.adapters`, `apps.notifications.worker`, `apps.core.commands`).
- **Other rules** (`policy.py`): `is_superuser` is read only in `apps/access` and grants no business capability; only a superuser acts on a staff account; nobody approves their own request unless the branch sets `allow_self_approval`.

## Guardrails and the acceptance matrix (step M2.4)

- **Static guard** (`scripts/check_access_rules.py`, CI `lint` job, stdlib only). Text rules: `|safe`, `mark_safe`, `csrf_exempt`, `.raw(`, `.extra(`, `is_superuser` in templates, and the `pull_request_target` / `workflow_run` triggers. Parsed rules over `apps/` and `config/` (comments and docstrings never match):
  - `scoped-model-access`: a scoped model's manager (`objects`, `_default_manager`) or `get_object_or_404(<ScopedModel>, …)` in views, forms or template tags. A scoped model is any model whose manager is built on `ScopedQuerySet`, found by reading the models, so document models are covered as soon as they exist;
  - `is_superuser`: an attribute, lookup, keyword or `getattr` outside `apps/access`;
  - `service-without-authorize`: a public function in a `services` module that writes (`@transactional`, an ORM write, `apply_changes`) without reaching `authorize_in_txn` directly, through its module's helpers or through another service module's function. Helpers that write for an authorized caller are private (`_elevate_privilege`);
  - `get-view-calls-service`: a view function or class that reaches a service without being POST-only (`@mutating`, `@require_POST`, `post_only=True`, `require_http_methods(["POST"])`, `MutatingViewMixin`, `http_method_names = ["post"]`);
  - `plain-model-admin`: a class based on `ModelAdmin`, or `site.register(Model)` without an admin class (the base itself is allowlisted);
  - `principal-constructor`: `TokenPrincipal`, `SystemPrincipal` or `OperatorPrincipal` constructed outside the modules listed in `CONSTRUCTOR_MODULES`, which the guard reads from `apps/access/principals.py` (an unreadable registry fails the check);
  - `mail-outside-outbox`: `smtplib`, or Django's `send_mail`, `send_mass_mail`, `mail_admins`, `mail_managers`, `EmailMessage`, `EmailMultiAlternatives` or `get_connection` outside `apps.notifications.worker`.

  The runtime checks stay: `gmat.E005` for admin classes, `PrincipalNotAllowed` in the principals' constructors.
- **Methods.** Every URL answers 405 to anything but GET, HEAD and POST (`AllowedMethodsMiddleware`, inside `ErrorPagesMiddleware` so the 405 has its page); views narrow further.
- **Shared fixture** (`tests/org.py`, plan §8.1): the A/B/C organization of §3.2, one user per role, a user who is material manager in A-SEV and technician in A-MAD, expired, revoked, revoked-assignment, inactive-branch, inactive-company-partner and deactivated users, and sentinel values (a price, a supplier's name, email and phone, another project's quantity). Numbers are matched in localized renderings (`98.765,43`, `98,765.43`, grouped with no-break spaces), names case-insensitively, phones with or without the country code. Suppliers (M3.2) and orders (M5.1) seed them.
- **Acceptance matrix** (`tests/access/test_acceptance_matrix.py`, plan §8.2): one row per URL pattern (keyed by URL name, or by route when unnamed) with the expected status and redirect target for GET, HEAD and POST, for each of 20 actors: anonymous, every role, the mixed-membership user, privileged sessions with and without MFA, the platform operator, the revoked cases and a session whose password changed. Every response is checked for the sentinels its row does not allow. A URL without a row, or a row without a URL, fails the test; other methods are checked to be 405 everywhere. URLs with an object list one row per object position (own scope, managed branch, foreign scope). The admin is checked too: every admin service-backed, access and document models read-only, no bulk delete, a master-data save through its service (generation bumped, audit written), a group administrator refused on a staff account, and an admin write refused after a membership expiry or revocation, a password, email or MFA change, a deactivation or an elevation without step-up between page load and submit.
- **The matrix checks itself** (`tests/access/test_matrix_guards.py`): views that commit each violation of M2.4's "done when" (an unscoped view, an unregistered URL, a leaked price, a GET view calling a service, a system principal in a view, a plain `ModelAdmin`) make it fail.
- Found by the matrix: the passkey list crashed for every signed-in user (`humanize` missing), and the admin's own password-change form bypassed allauth. Both are fixed.

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

## Email: the outbox and the worker (step M2.5)

Code in `apps/notifications/`; plan §5 "Email: outbox and worker".

- **No SMTP in a request.** `MAILERS["default"]` is `OutboxEmailBackend` in every environment (checks `gmat.E007`, and `gmat.E017` in production): Django's and allauth's mail becomes `EmailOutbox` rows in the caller's transaction. Business code queues through `apps.notifications.outbox` (`queue_notification`, `queue_security_email`, `queue_dispatch`). The static guard keeps `smtplib` and Django's sending API inside `apps.notifications.worker`.
- **Frozen content, re-checked recipients.** Subject, bodies, recipients and eligibility are frozen at insert by a trigger (`notifications.0002`; a body may only be wiped). Notifications store user ids and a capability on an object, at most 20 per message (BCC); the worker re-checks each recipient's own access at the database clock of the claim and resolves addresses from the current user records, dropping who no longer qualifies (`SUPPRESSED` when nobody is left). allauth's mail is a `SECURITY` email to one account, sent only while that account is active and still has the address.
- **Sensitive bodies** (security emails) are sealed with `OUTBOX_ENCRYPTION_KEYS` (MultiFernet, their own keys; never an MFA key, `gmat.E018`), wiped as soon as the message is sent or can no longer be sent, and wiped and `EXPIRED` one hour after queueing if still unsent (hourly housekeeping).
- **The worker** (`manage.py worker`, `entrypoint.sh worker`) is the only sender and runs as `system:worker` (`outbox.send`, `housekeeping.*`, no business capability). Each message: claim in a short transaction (`FOR UPDATE SKIP LOCKED`; a supplier dispatch locks its order first, L4 → L4b), eligibility re-check, an `EmailAttempt` committed **before** any network I/O; send with a 10 s timeout per socket operation (`OUTBOX_SMTP_TIMEOUT`; Django reserves `EMAIL_TIMEOUT`) and a 60 s total deadline enforced by `SIGALRM`; the outcome is recorded only while `current_attempt` is still that attempt. `FAILED` = refused connection or definite rejection before DATA; `AMBIGUOUS` = timeout or disconnect after DATA started. An attempt without outcome 90 s after it started (worker crash) becomes `AMBIGUOUS`, checked at start-up, every minute and exactly when the oldest attempt in flight becomes due. Retries: notifications `FAILED` up to 5 attempts with backoff and `AMBIGUOUS` once; security emails `FAILED` within their hour, never after `AMBIGUOUS`; supplier dispatches never after `AMBIGUOUS`. Errors record a stage and an SMTP code, never the server's text; logs carry message ids only.
- **Switches.** `flags/worker.paused`: the worker only writes its heartbeat (no query, claim, send or housekeeping). `OUTBOUND_EMAIL=off` (default in production until set): it never claims or opens an SMTP connection. Restore drills set it off.
- **Housekeeping** (hourly, one transaction and one `housekeeping.<task>` authorization per task): idempotency records past retention, expired security-cache rows, expired sensitive bodies. M6.4 adds the attachment tasks.
- **Containers.** The worker shares no network with `web` (`db_worker` and `egress` only); the SMTP password reaches the worker container only. Development, local and CI stacks deliver to Mailpit, so no mail leaves the host.

## System checks (`apps/core/checks.py`)

| Id | Refuses |
| --- | --- |
| `gmat.E001` | allauth below the floor |
| `gmat.E002` | weakened allauth or MFA settings (enumeration, logout on GET, login on reset, login by code, unknown-account emails, trusted browsers, passkey login, TOTP tolerance or bypass code, proxy count) or an open sign-up |
| `gmat.E003` | a default cache other than `SecurityDatabaseCache` |
| `gmat.E004` | a logging handler without redaction, or an email handler on Django's loggers |
| `gmat.E005` | a `ModelAdmin` not built on `ServiceBackedModelAdmin` |
| `gmat.E006` | idempotency records kept no longer than their keys are valid (a purge could turn a replay into a new command) |
| `gmat.E007` | a default mailer that is SMTP (or none): requests must only queue mail |
| `gmat.E010`–`E017` (deploy) | `DEBUG`, a weak or placeholder `SECRET_KEY`, missing or development `MFA_ENCRYPTION_KEYS`, empty or wildcard `ALLOWED_HOSTS`, non-https `CSRF_TRUSTED_ORIGINS`, cookies that are not `Secure __Host-`, a first hasher other than Argon2, a default mailer other than the outbox |
| `gmat.E018` (deploy) | missing, invalid, development or MFA-shared `OUTBOX_ENCRYPTION_KEYS` |
| `gmat.E019` (deploy) | a worker mailer (`MAILERS["outbound"]`) that is not SMTP, has no timeout or more than `OUTBOX_SMTP_TIMEOUT`, a send deadline not longer than one operation, or an abandon grace under 30 s |

`manage.py check --deploy` passes with zero warnings under production settings, the compose stack's included.

## Images, database roles and backups (step M1.4)

Sources: `docker/app/`, `docker/backup/`, `docker/postgres/initdb/`. CI builds both images and holds them to the Trivy gate (`trivy-image`).

- **Application image** (`docker/app/Dockerfile`): `python:3.14-slim` pinned by digest, stages `assets` (Tailwind standalone CLI, sha256-verified) → `builder` (`uv sync --frozen --no-dev`, hashes from `uv.lock`) → `runtime`, plus a `dev` target. Runs as uid/gid 1000 on a read-only root filesystem; source is owned by root; pip is removed; `collectstatic` runs with `config.settings.build` (no database, no secret). `entrypoint.sh` is the only entry point (`web`, `worker`, `worker-healthcheck`, `migrate`, `check`, `migrate-plan`, `healthcheck`, `manage`; `verify-internal` and `post-restore-reconcile` arrive with M8.4).
- **Gunicorn** (`gunicorn.conf.py`): `gthread`, access log off (Caddy is the access log), no control socket, and `FORWARDED_ALLOW_IPS` must list the edge subnet: it refuses to start with a wildcard or an empty value. Its error log uses the same JSON formatter and redaction filter as Django.
- **Database roles** (`10-roles.sh`, run once at database initialisation, idempotent): `owner` (owns the schema; used only by the one-shot `migrate`), `runtime` (SELECT/INSERT/UPDATE/DELETE only; owns nothing, so it cannot alter or drop tables, triggers or the audit table) and `backup` (`pg_read_all_data`, read-only sessions). All use `scram-sha-256`. The script refuses placeholder or short passwords, repeated names or passwords, and a role password equal to the bootstrap superuser's.
- **Backups** (`docker/backup/`): every 6 hours and on demand (`backup-now [--label]`). A psql session takes the advisory lock `gmat:media-backup` (key `hashtextextended('gmat:media-backup', 0)`; the worker's media purge uses the same key with `pg_try_advisory_lock`), opens a `REPEATABLE READ READ ONLY` transaction and exports its snapshot. `pg_dump -Fc --snapshot` leaves out the data of `django_session` and `django_cache`, and the manifest queries (row counts of every table, migration head, newest audit event) run in the same snapshot, so dump and manifest agree under concurrent writes. The media directory, when there is one, is copied under the lock and checksummed. Files are mode 0600 in a 0700 directory and kept 3 days locally. The offsite upload uses restic with a fixed host and tags and **never prunes**; `forget`/`prune` run only from the maintainer's machine (plan §9).
- **`restore --verify`** (weekly, and on demand) restores the newest dump into a private PostgreSQL started inside the backup container on a unix socket (no network, no write right on the real database), then compares every row count, the migration head and the newest audit event with the manifest. Tables whose data is excluded must come back empty.
- **Exceptions** in `.trivyignore.yaml` are Debian base-image packages with no fixed version yet; each has a statement, an owner and an expiry at most 90 days away, and needs the maintainer's explicit approval in the PR.

## The compose stack (step M1.5)

Files in `docker_compose/gmat_m8/` (see its README); `tests/live` proves the points below against a running stack and the `compose-smoke` workflow runs it.

- **Networks.** One internal network per database client (`db_web`, `db_worker`, `db_backup`, `db_migrate`), so `web`, `migrate`, `backup` and the worker share none and cannot reach each other. `edge` is internal and shared only by Caddy and `web` (no route out for `web`); Caddy alone sits on `ingress`, the one network that publishes ports. The database is never published (the development overlay publishes it on 127.0.0.1 only).
- **Hardening.** `cap_drop: ALL`, `no-new-privileges`, read-only root filesystem, memory and process limits and log rotation on every service; `web`, `worker`, `migrate`, `backup` and the local Mailpit run as uid 1000. Caddy runs as root inside its container with only `NET_BIND_SERVICE` (its data volumes are root-owned; nothing else is writable).
- **Caddy.** Admin API off; `read_header` 10 s and `read_body` (`CADDY_READ_BODY_TIMEOUT`, 120 s) so a client that stops sending frees its Gunicorn thread; body limit `MAX_UPLOAD_SIZE`; no trusted proxies, so a client-sent `X-Forwarded-For` never reaches Django; `Server` header removed; HTTP/3 only where the UDP port is published. The admin prefix (`ADMIN_URL_PATH`, the same value in Django) is limited to `ADMIN_ALLOWED_CIDRS`.
- **Maintenance gate.** `flags/maintenance.on` makes Caddy answer 503 with the maintenance page for every public request while `web` keeps running; the same page replaces a missing `web` (502/503/504).
- **Log redaction.** Caddy's default and access logs replace the token paths (`/accounts/password/reset/key/…`, `/account/email-change/…`, `/invite/…`) and every query string with `REDACTED` and drop `Referer` and `HX-Current-URL`. Only paths under those prefixes are secrets to Caddy; Django's own filter also redacts any long random segment.
- **Secrets.** `init.sh` generates every secret (mode 0600, never printed, redrawn if it resembles a placeholder). A secret is defined once in `.env` and reaches the services by interpolation; Django-only settings are in `app.env`. Only `*.example` templates are committed.
- **Worker.** `db_worker` and `egress` only, `./flags` mounted read-only, a heartbeat healthcheck (`worker-healthcheck`, also while paused) and 75 s to stop, so a send in flight can finish. Local and CI stacks add Mailpit (`docker-compose.local-mail.yml`); production never loads it.
- **Not yet.** `deploy.sh` and `recover.sh` (M8.4), which will verify the signed release manifest and image digests that the publish pipeline (M1.7, [release.md](release.md)) produces before writing them into the production overlay.

## The UI shell (step M1.6)

Templates in `templates/`, styles in `static/src/app.css`, code in `static/js/app.js` and `static/vendor/`.

- **No inline code.** Pages carry no inline script, style or event handler (`tests/unit/core/test_ui_shell.py` scans the rendered login, home and error pages; a real-browser run against the stack showed zero console and CSP errors). The only `<script>` blocks are allauth's `application/json` data blocks, which the CSP does not execute.
- **htmx** 2.0.11 is vendored (`static/vendor/README.md` records its source and checksum; a test pins the sha256). It is configured by `<meta name="htmx-config">`: `historyCacheSize: 0`, `refreshOnHistoryMiss`, no eval, no script tags, no indicator styles, `selfRequestsOnly`. The CSRF token travels in `hx-headers` on `<body>`; the cookie is `HttpOnly`. Nothing is written to `localStorage` (checked in the browser).
- **Static files** are hashed (`CompressedManifestStaticFilesStorage`) and served by WhiteNoise with a far-future cache header; the Tailwind input is not shipped.
- **Language**: Spanish by default, English and French; the choice is a `__Host-language` cookie (`Secure`, `HttpOnly`, `SameSite=Lax`) set by a POST form. The form's `next` is the current path, except on a token URL, where it is `/` so a secret is never copied into another form. Catalogs are in `locale/`; the `.mo` files are compiled in the image build.
- **Error pages** 400, 403 (including the CSRF failure), 404, 405, 409, 429 and 500 show a code and a generic sentence, never a path, a query, an exception or a user. 405, 409 and 429 are produced by `ErrorPagesMiddleware` for bare responses; 500 renders without a request.
- **Referrer policy.** `no-referrer` only on a URL that carries a token. A form page keeps `same-origin`: under `no-referrer` browsers send `Origin: null` on its POST and Django's CSRF check refuses it, so login would fail. (Found by logging in with a real browser; the M1.3 tests asserted the header, not the login.)

## Still to come

- M2.5: outbox, worker, and the cache housekeeping (`SecurityDatabaseCache.purge_expired`).
- M2.6: invitations, administrator email changes, offboarding, `bootstrap_admin`; their token pages join the no-referrer list and the log redaction prefixes.
- M8.2: the same checks in a real browser (e2e).
