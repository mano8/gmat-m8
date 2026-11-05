# Security review

A record of each review pass: what was checked, how, what was found and what became of each
finding. The controls themselves are described in [security.md](security.md). The deviations
the maintainer accepts, each with a date to review it again, are in its
[exception register](security.md#exception-register).

## Pass 1: step M8.3 (2026-11-04)

Reviewed at `aebc8f0` (`m8-hardening` after M8.2); the fixes are the `[M8.3]` commits that
follow it.

### Method

- **`/security-review` of the whole codebase.** The review's method has three steps: sub-tasks find candidates, one further sub-task per candidate filters false positives, and only findings with a confidence of 8/10 or more are kept. It ran over six areas:
  - authentication, sessions and account tokens;
  - the access engine, the admin, and the people, project, master-data, audit, export and dashboard screens;
  - the document apps (requests, purchasing, receiving, handovers);
  - attachments, uploads and imports;
  - email, logging, idempotency keys, list parameters, raw SQL, templates and JavaScript;
  - images, compose, Caddy, database roles, backups, CI and release tooling.

  Each area reviewer also listed places where the code contradicts this documentation. The review's own exclusions (denial of service, rate limiting, secrets on disk) are covered by the sweep instead.
- **A checklist drawn from OWASP ASVS 5.0, level 2**, for the areas plan step M8.3 names.
- **The sweep of the step card**: scans and probes run locally, and the security tests.
- **Every finding of the plan's review log (F1–F81) re-checked** against the code and the tests.

### Findings and what became of them

| # | Severity | Finding | Resolution |
| --- | --- | --- | --- |
| R1 | Medium (8/10) | The Django admin let a list be filtered on any local field. Reading the result count, one guess at a time, a group administrator could recover any account's password hash (staff accounts included). A platform operator could recover prices the same way (order lines, audit payloads). | Fixed in `12afb42` |
| R2 | Medium (8/10) | An over-receipt at a worksite was refused with "only N remain". N is the order line's whole outstanding quantity, so it gave a site manager other projects' quantities. The order of the refusals revealed the same thing. | Fixed in `d7b48ed` |
| R3 | Low (5/10, below the report's bar) | The admin's audit pages showed event payloads (price changes) and client addresses to platform operators. | Fixed in `12afb42` |
| R4 | Low | Caddy logged the response's `Location`. A token link opened without its trailing slash gets Django's 301 to the token URL, so the live token reached the access log (reproduced on the local stack). | Fixed in `9280864`, live test extended |
| R5 | Low | allauth's passkey removal never asked whether a privileged account keeps a second factor. | Fixed in `5cf36de` |
| R6 | Low | A token link over its rate limit was answered with 429 at the token URL, which kept the token in the address bar. | Fixed in `d15f8fa` |
| R7 | Low (ASVS V16.3) | Sign-ins, failures, sign-outs, password and factor changes were not logged, and log lines did not name the account. | Fixed in `2161284` |
| R8 | Low | There was no supported, audited way to reset a lost second factor. | Fixed in `b266ce5` (`manage reset_mfa`) |
| R9 | Low | HSTS `preload` was on by default, against plan §5. The production template's `false` made `check --deploy --fail-level WARNING` fail. | Fixed in `4b51ff5`; exception X3 |
| R10 | Low | The nightly scan trusted an unsigned manifest of whatever release came first, a prerelease included. | Fixed in `80decd3` |
| R11 | Low | The local agent guard let `gh api` writes through, since `-f` implies POST (a release, a tag ref, a workflow dispatch), as well as tag pushes by refspec. | Fixed in `3b0e286` |
| R12 | Low | PostgreSQL's slow-statement log carried literal values (names, addresses). | Fixed in `9280864` |
| R13 | Low | Role passwords were on the `psql` command line during database initialisation. | Fixed in `33b7d65` |
| R14 | Info | One Trivy exception was stale. | Dropped in `dec8626` |
| R15 | Low | The admin prefix is open to every network until `ADMIN_ALLOWED_CIDRS` is set. | Exception X10 |
| R16 | Low | A receipt's files (delivery-note photos) reach the project's site manager. | Exception X8 |
| R17 | Info | Documentation drift: <br>• the worker and the backup share `egress`; <br>• engineers may remove attachments; <br>• `seed_demo`'s guard; <br>• the purge lock's name; <br>• gmat.E020 assumes Caddy's 120 s; <br>• removed files count only until the purge; <br>• the DEBUG error page; <br>• the replay docstring of `apps/core/idempotency.py`. | Corrected in the documentation commit |
| R18 | Question | A reviewer without a self-deciding role can return their own request for changes, or reduce its approved quantity. The service docstring says no. | Question 1 below |
| R19 | Low | A project assignment's rules on its assignee are enforced by the form only, not by `assign_to_project`. | Question 2 below |
| R20 | Info | The catalog and supplier services lock the master-data row before `authorize_in_txn`. This is lock order only, with no effect on access. | Noted, not changed |

The ASVS gaps that remain are exceptions X4–X7, X9, X11 and X13.

### The `/security-review` report

Findings kept after the false-positive filter (confidence ≥ 8/10). Line numbers are those of `aebc8f0`.

#### Vuln 1: data_exposure (password-hash oracle in the admin): `apps/accounts/admin.py:24`

- Severity: Medium. Confidence: 8/10.
- Description: `UserAdmin` is the project's own read-only admin (not Django's `auth.admin.UserAdmin`) and inherits Django's `ModelAdmin.lookup_allowed`, which accepts any lookup on a local field. `exclude = ("password", ...)` hides the field from the form but not from the changelist's filters. The same applies to `token_hash` and `cancel_token_hash`, `Authenticator.data`, `PurchaseOrderLine.unit_price` and `AuditEvent.data`.
- Exploit scenario: a group administrator with a session that passed MFA sends `GET /<admin>/accounts/user/?email=victim@…&password__startswith=argon2$argon2id$…$<guess>` and reads "1 user" or "0 users". A few hundred requests per account recover the Argon2id string, which is then cracked offline; a weak password of an account without MFA then lets them sign in as that person. Query strings never reach the logs, so this leaves no trace.
- Recommendation and fix: `ServiceBackedModelAdmin.lookup_allowed` accepts only the fields of the admin's list filters and date hierarchy (`12afb42`). `tests/unit/core/test_admin_lookups.py` checks that every hidden column is refused (400) and that every list filter of every admin still works.

#### Vuln 2: data_exposure (site-manager projection): `apps/receiving/services.py:232`

- Severity: Medium. Confidence: 8/10.
- Description: `_check_capacity` refused an over-receipt with "Line N: only X remain to be received". X is the order line's outstanding quantity across every project it serves. The receipt form shows a site manager only what their project can take (§3.4), and its quantity field has no upper bound.
- Exploit scenario: a site manager posts `qty_<line>=99999` at their project's worksite (`POST /receiving/orders/<po>/post/`) and reads X on the form, which comes back with status 200 and nothing written. X minus their own quantity is the other projects' quantity. The request can be repeated without a trace. With the figure gone, the order of the checks (capacity before splits) still gave a yes/no answer for a binary search.
- Recommendation and fix: the message carries no quantity, and capacity is checked only after every line and split is valid (`d7b48ed`). Two view tests post as a site manager.

### Sweep

| Item of the card | What was done | Result |
| --- | --- | --- |
| IDOR checks driven by the matrix | Ran `tests/access` (every URL × GET/HEAD/POST × 20 actors, object positions, revoked actors, sentinels). Asked the review to look for what the matrix does not exercise. | Green. The review found two paths the matrix does not exercise: an over-receipt POST (R2) and the admin's query parameters (R1). Both now have tests. |
| Headers, CSP and TLS | `curl` through Caddy on login, a token link, password reset, a 404, the admin and a static file. `openssl s_client` per protocol. | CSP, `frame-ancestors 'none'`, COOP, `nosniff`, `X-Frame-Options: DENY`, `Permissions-Policy`, `Referrer-Policy` (`no-referrer` only on the token URL), `__Host-` cookies that are `Secure`, `HttpOnly` and `Lax`, `no-store` on pre-authentication and token pages, HSTS. TLS 1.0 and 1.1 are refused by the server (alert 70); TLS 1.2 is ECDHE with AES-GCM; TLS 1.3 is offered; an unknown SNI is refused. R9 is fixed. |
| Admin and MFA boundaries; the read-only admin | Reviewed `admin_site`, every `ModelAdmin`, the MFA middleware and adapter. Ran the admin tests. | R1, R3, R5 and R8 fixed. 55 admin unit tests and 612 admin matrix rows green. |
| Principals and token flows | Reviewed the constructor allowlist, the token principals' locks and checks, and the reset re-validation under lock. | No finding. |
| Invitation tokens, email changes, offboarding | Reviewed hashing (32 bytes, SHA-256, `compare_digest`), single use, the initiator re-check, the cooling-off and cancel, and offboarding's revocations. | R6 fixed. No other finding. |
| Log redaction | Reviewed the Django filter, Gunicorn and Caddy. Probed the stack with sentinel tokens. | R4 fixed; before and after the fix were checked on the local stack. The live sentinel test now covers links opened without their trailing slash. Django and Gunicorn had no gap. |
| Cache saturation and parallel rate limits | Ran `test_security_cache.py`, `test_security_cache_races.py` and `test_login_rate_limit.py`. Read allauth 65.20.1's MFA rate limit. | Green. MFA codes count against `login_failed` per account (5 per 5 minutes) and per IP. |
| Outbox secrecy and eligibility | Reviewed sealing and wiping, the claim-time eligibility, the BCC fan-out, header injection (Django 6.1's email policy) and STARTTLS certificate checks. | No finding. TLS is not enforced by a check: X13. |
| Dependency and image scans; `.trivyignore.yaml`; security floors | Trivy 0.75.0 with the database of 2026-11-04 on both images and the repository. `pip-audit` on `uv export --frozen`. bandit at medium. gitleaks v8.28.0 over the full history. Every action pin checked against GitHub. | The application image has 9 HIGH findings, all with exceptions and all still without a fix upstream; one stale exception was dropped (R14). The backup image and the repository are clean, with no secret and no high misconfiguration. `pip-audit` finds no known vulnerability. bandit: no medium or high finding (one low false positive). gitleaks: 166 commits, no leak. 13 of the 14 action pins equal their tag's commit; `trivy-action` is pinned to its verified `master` head (Dependabot #16, the Trivy binary pinned to v0.75.0). The allauth floor is 65.20.1, which is installed, with no advisory. |
| Secrets, backup encryption and append-only offsite storage | Reviewed `init.sh` (urandom, umask 077, never printed), the secrets each container receives, and the database roles (DML-only runtime, read-only backup). Reviewed `backup.sh`: snapshot and lock, sessions and cache excluded, files 0600 in a 0700 directory, restic never prunes. | R13 fixed. Local copies are unencrypted (X12). The append-only configuration on the storage side and the prune script on the maintainer's side are M8.4 and H8.6. |

### ASVS 5.0, level 2

Each row is the requirement in the project's words, its verdict and where the evidence is.

| Area | Requirement | Verdict | Evidence |
| --- | --- | --- | --- |
| Authentication (V6) | Passwords of at least 12 characters, long passphrases allowed, no composition rules; common and similar passwords refused | Met | `AUTH_PASSWORD_VALIDATORS` |
| | Passwords checked against a breached-password set | Exception X4 | |
| | Argon2id storage | Met | gmat.E016 |
| | A password change needs the current password and ends the other sessions | Met | allauth, the `session_epoch` trigger |
| | Sign-in, MFA codes, reset, reauthentication and token links are rate-limited; the security cache fails closed | Met | `ACCOUNT_RATE_LIMITS`, `SecurityDatabaseCache`, parallel tests |
| | No account enumeration through sign-in or reset | Met | gmat.E002 |
| | No default or shared account; the first administrator by a one-time link | Met | `createsuperuser` disabled, `bootstrap_admin` |
| | MFA on every privileged role, judged per session and after each elevation | Met | `MFARequiredMiddleware`, `authorize_in_txn` |
| | TOTP: one time step, single use, secrets encrypted at rest; recovery codes single use | Met | `MFA_TOTP_TOLERANCE = 0`, `MFAAdapter` |
| | A privileged account always keeps a factor; a lost factor is reset by an audited administrator action | Met (M8.3) | R5, R8 |
| | Password reset: single use, 1 h, re-validated under the user lock, never signs in, refused for inactive accounts | Met | `AccountAdapter.redeem_password_reset` |
| | Users are told when their authentication details change | Partly: email changes only | Exception X9 |
| | Enrolment and recovery links: 256-bit, hashed, single use, expiring, out of the URL on first use | Met (M8.3) | `tokens.py`, R6 |
| Sessions (V7) | Server-side sessions, random ids, rotated at sign-in, flushed at sign-out | Met | Django sessions, POST-only logout |
| | Cookies `Secure`, `HttpOnly`, `SameSite=Lax`, `__Host-` | Met | live headers, gmat.E015 |
| | Absolute lifetime (12 h privileged, 14 days otherwise) | Met | `SessionGuardMiddleware` |
| | Inactivity timeout | Documented decision | Exception X6 (Q12) |
| | Every session ends on a password change, an MFA reset, an email change or a deactivation | Met | `session_epoch` |
| | Users can see and end their other sessions | Not met | Exception X5 |
| | Reauthentication before changing a factor | Met | allauth (65.20: only an explicit reauthentication counts) |
| Access control (V8) | Capability matrix documented as data and tested against the plan | Met | `roles.py`, its test |
| | Deny by default; checked server-side per function (views), per object (selectors) and per write (services) | Met | `require()`, `visible_to`, `authorize_in_txn`, the matrix |
| | Field-level rules: prices only with `price.view`; the site-manager projection | Met (M8.3) | sentinels in the matrix, R2 |
| | A revocation or an expiry applies to the next write | Met | `test_access_staleness.py` |
| | Company, branch and partner boundaries | Met | S1–S9, the sentinel matrix |
| | Admin interfaces need privilege and MFA, and leak no hidden column | Met (M8.3) | `SecureAdminSite`, R1, R3 |
| | Admin reachable only from allowed networks | Configurable | Exception X10 |
| Input and output (V1, V2) | Queries through the ORM; raw SQL parameterised; no `.raw()` / `.extra()` | Met | `check_access_rules.py`, area E review |
| | Output escaped by the template engine; no `\|safe` / `mark_safe` | Met | the guard |
| | CSP without inline script or style; htmx without eval or script tags | Met | settings, e2e CSP listener |
| | Choices come from selectors; forged ids refused | Met | forms, matrix |
| | Business rules and quantity invariants enforced in services, under concurrency | Met | M8.1 suite |
| | No unsafe deserialisation: JSON only; XLSX through `defusedxml`; no pickle or YAML on input | Met | `cache.py`, `idempotency.py`, `tabular.py` |
| | CSV formula injection neutralised | Met | `exports.cells` |
| | No mail header injection | Met | Django 6.1's email policy, recipients stored as user ids |
| Logging (V16) | JSON lines with time, level, request id and the signed-in account | Met (M8.3) | `config/logging.py` |
| | Authentication events logged: sign-in, failure, sign-out, password and factor changes, refused MFA codes | Met (M8.3) | `apps/accounts/security_log.py` |
| | Refused requests (403, 404) logged with who made them | Met (M8.3) | `django.request` plus the account id |
| | No token, password or needless personal data in any log | Met (M8.3) | redaction at every layer, sentinel tests at three levels, R4, R12 |
| | Business audit trail append-only; the runtime role cannot alter it | Met | trigger, database roles |
| | Generic error pages; no stack trace; DEBUG refused in production | Met | error pages, gmat.E010 |
| Files (V5) | Size limits before the body is read; count and quota checked atomically | Met | `uploads.py`, L8 |
| | Type from magic bytes; images re-encoded under a pixel limit in a killable, rlimited child | Met | `processing.py`, `decoder.py`; exception X11 |
| | Stored outside the web root under random names; never served directly | Met | `files.py`, the download view |
| | Download as an attachment with `nosniff` and a sandbox CSP | Met | `views.download` |
| | Malware scan of stored files | Not met for PDFs | Exception X7 |
| Email | No SMTP in a request; the outbox is written in the business transaction | Met | gmat.E007, E017 |
| | Recipients re-checked at the claim; BCC; frozen content | Met | `eligibility.py`, trigger |
| | Security mail sealed, wiped once sent, expired after 1 h | Met | `crypto.py`, housekeeping |
| | Links built from `PUBLIC_BASE_URL`, so no Host-header poisoning | Met | settings, `ALLOWED_HOSTS` |
| | TLS to the relay, certificate checked | Met by default | Exception X13 |
| Administration | Admin sign-in through allauth with session MFA; group administrators and superusers only | Met | `admin_site.py` |
| | Access and documents read-only; master data saved through services; no delete | Met | gmat.E005, guard, matrix |
| | Group administrators never act on staff accounts | Met | `require_may_manage_account`, including the MFA reset |
| | Break-glass operations are audited operator commands | Met (M8.3) | `bootstrap_admin`, imports, `reset_mfa` |
| Configuration and transport (V3, V12, V13) | TLS 1.2 or later only; one-year HSTS; HTTPS redirect | Met | live probe; preload off (X3) |
| | Production settings fail closed; `check --deploy` gives zero warnings | Met | `config/env.py`, gmat.E010–E020 |
| | Secrets only in env files, generated and kept 0600; MFA and outbox keys separate | Met | `init.sh`, gmat.E018 |
| | Dependencies locked with hashes; security floors; scans in CI | Met | `uv.lock`, `ci.yaml`; exception X1 |

### Plan review findings re-checked (F1–F81)

The plan's review log is local to the maintainer; each id is listed here once, with where it is enforced now.

| Findings | Topic | Enforced by | Verdict |
| --- | --- | --- | --- |
| F1, F21, F46, F56, F66 | Every write authorized in its transaction; non-session principals; POST-only writes | `apps/access/authorization.py`, `principals.py`, `check_access_rules.py`; `test_access_staleness.py`, the matrix | Holds |
| F2, F36 | Site-manager receipts and their projection | `receiving` selectors and services; matrix sentinels | Holds (R2 fixed) |
| F3, F29 | Cross-document integrity; a unit change racing the first reference | composite keys C1–C10, triggers; `test_receiving_constraints.py`, the invariant checker | Holds |
| F4, F55 | Canonical lock order, the order before its dispatches | `apps/core/locking.py`; race tests | Holds |
| F5, F70 | Editing by state; the floor of an approval reduction | the request services, ADR-0014; their tests | Holds |
| F6, F25, F26, F37–F40, F51, F57, F65, F69 | The quantity ledger: pending, attribution, reconciliation, caps | `purchasing/components.py`, `waterfall.py`, `receiving/attribution.py`; `tests/invariants.py`, `test_ledger_machine.py` (M8.1) | Holds |
| F7 | Closed documents, voids, direct handovers | `transitions`, `handovers/direct.py`, triggers | Holds |
| F8, F27, F58 | Rate limits on a shared, atomic cache; the client address | `apps/core/cache.py`, `http.py`; cache and sign-in tests | Holds |
| F9, F68, F78 | Admin sign-in through allauth; session MFA and elevation; encrypted TOTP; the allauth floor; no sign-in on reset or by code | `admin_site.py`, `mfa.py`, `adapters.py`, gmat.E001/E002 | Holds (R5, R8 fixed) |
| F10, F41, F71, F76, F77 | Invitations, offboarding, the initiator re-check, bootstrap, the cooling-off | `accounts` services and tokens; their tests | Holds |
| F11, F30 | Idempotency bound to actor, operation and payload; records outlive keys | `apps/core/idempotency.py`, gmat.E006 | Holds |
| F12, F28 | Tokens out of every log; `no-referrer` and `no-store` on token pages | `config/logging.py`, the Caddyfile, `SecurityHeadersMiddleware`; sentinel tests (unit, live, e2e) | Holds (R4, R6 fixed) |
| F13 | No history snapshot or cached page after sign-out | the htmx configuration, `no-store`; e2e `test_browser_guards` | Holds |
| F14, F31, F45, F48, F54, F73 | Attachments: permissions, atomic quota, leases, purge coordination, enforced time limits, admission before the body | `apps/attachments`; `test_attachment_races.py` | Holds |
| F15, F32, F33, F42, F47, F53 | Outbox and worker: claims, deadline, eligibility, abandoned attempts, cancellation notices | `apps/notifications`, `purchasing/dispatch.py`; `test_outbox_worker.py`, `test_po_dispatch.py` | Holds |
| F16, F22, F49 | The publication gate and the agent identity | `docker-publish.yaml`, `verify_release.py`, rulesets; the interim setup (Q20) | Publication holds; agent boundary is exception X2 (guard tightened, R11) |
| F17 | Trivy without `--ignore-unfixed`; dated exceptions | `ci.yaml`, `image-scan.yaml`, `check_trivyignore.py` | Holds (R14) |
| F18, F23, F43 | Deploys: the running `web`, serialisation, the worker before verification | the maintenance gate and `worker.paused` exist; `deploy.sh` is M8.4 | Partly; the rest is M8.4 |
| F19, F34, F63 | Backup verification; the manifest in the dump's snapshot; sessions and cache left out | `backup.sh`, `restore.sh --verify`; live backup test | Holds; the offsite drill is M8.4 and H8.6 |
| F20 | No workflow-level path filter; the matrix covers every URL | `compose-smoke.yaml` (scope and gate); `test_acceptance_matrix.py` | Holds |
| F24, F44, F52 | Restores: sessions, credentials, invitations, revoked access, recovery codes, re-sent mail | the drill overlay (no egress, outbound mail off) exists; `post-restore-reconcile` is M8.4 | Scheduled (M8.4) |
| F35 | Both digests published; e2e on the exact commit | `docker-publish.yaml`, `verify_release.py` | Holds |
| F50 | Partners of an inactive company | `scopes.py` (S9); its tests | Holds |
| F59 | No self-service email change | the disabled allauth views; tests | Holds |
| F60 | The admin never bypasses services | `ServiceBackedModelAdmin`, gmat.E005, the guard, the matrix | Holds (R1, R3 fixed) |
| F61, F72 | Append-only offsite storage; retention poisoning | `backup.sh` never prunes; storage settings and the prune script are M8.4 and H8.6 | Scheduled (M8.4) |
| F62 | Deactivation preconditions | `organizations/deactivation.py` | Holds |
| F64, F74 | One database network per client; the worker off `edge` | compose networks; live tests | Holds (the worker and the backup share `egress`: the documentation is corrected) |
| F67 | Hardening defaults | Caddy admin API off, scram-sha-256, 1 h reset tokens, no enumeration | Holds; data handling on the drill machine is runbook M8.5 |
| F75 | The drill overlay has no way out | `docker-compose.restore-drill.yml` | Holds (file); outbound test is M8.4 |
| F79 | The VPS's read-only GitHub token | step H8.6 | Human step |
| F80, F81 | Token entropy and comparison; `__Host-` cookies; `Permissions-Policy` | `accounts/tokens.py`; live headers | Holds |

### Questions for the maintainer

1. **R18.** May a reviewer without a self-deciding role, such as a site manager, return their own submitted request for changes, or reduce its approved quantity? The code allows it, while the docstring of `apps/material_requests/services/__init__.py` says decisions on one's own request need self-approval. Neither action gains the requester anything. Either the docstring or the code changes; no business rule was invented here.
2. **R19.** Should `projects.services.assign_to_project` check the assignee itself? That means the matching role's membership, a home branch in scope and an active account, which today only the form checks. A mismatched assignment grants nothing until the matching role is granted.
3. **X9.** Should allauth's notices of password and factor changes be turned on (`ACCOUNT_EMAIL_NOTIFICATIONS`)? The e2e mailbox helper takes the next message to an address, so its flows would need to skip these notices.
4. **R12.** The slow-statement log is off because statements carry their values. If it is wanted back, it needs server-side parameter binding, or acceptance of personal data in the database log.
