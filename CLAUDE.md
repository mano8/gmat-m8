# gmat-m8

Materials app for a group of electrical-installation companies and their branches: technicians request materials for worksites, managers approve, purchasing orders from suppliers, goods are received at a warehouse or on site and handed over. Modular Django monolith (Django 6.1 + HTMX + PostgreSQL 18), Docker Compose on one VPS.

## Source of truth

- **Plans:** markdown files in `.plans/todo/`; there can be several. Work from the plan the maintainer names; "plan §n" in this file means a section of that plan. Plans are local and git-ignored (missing in clones, CI and cloud sessions). If the named plan is missing, or none is named and the choice is unclear, stop and ask; never work from memory of a plan. Before a phase, read the plan's working rules, the phase's step cards and the sections they list.
- **Decisions:** the plan's decisions and answered questions; committed ones live in `docs/adr/` (from M1.1 on), and an ADR wins over any plan. For a question still open, use its working assumption and say so in the PR. Do not invent business rules.
- Keep this file current when commands, layout or rules change.

## Git, PRs, releases

- **One phase = one branch = one PR** (M1, M2, …). Branch: `git switch --no-track -c m<phase>-<slug> origin/main` (e.g. `m2-org-access`). Human steps (`H*`) are skipped. Commit per step with Conventional Commits + step id: `feat(access): resolve dependent branch scope [M2.3]`. Never `--no-verify`.
- **Release ordering (plan §0.2):** a step that publishes or releases (M1.7 `docker-publish.yaml`) never shares a PR with the code it builds. Open and merge the preceding PR first (M1: PR 1 = M1.1–M1.6 on `m1-foundation`), then cut `m1-release` from the updated `origin/main` for M1.7 in its own PR. Nothing is tagged, released or published before both are merged and H1.8 is done.
- Agents use the configured owner account (`gh`/git): commit, `git push -u origin <branch>`, open the PR with `gh pr create --repo mano8/gmat-m8 --head <branch> --draft`, answer reviews.
- **Never:** merge or approve a PR; push to `main` or force-push; create tags or GitHub Releases (`v*` tags publish images); run `gh workflow run` or `docker-publish`; deploy or touch production data; change repo settings, rulesets, secrets or tokens. GitHub will not stop the owner account, so these rules and `.claude/settings.json` are the guard.
- **No attribution:** no `Co-Authored-By` lines, no "Generated with" footers and no AI tool or model names in commits, PR titles, descriptions or comments.
- When a step's job is done (implemented with tests, committed, pushed, local gates green), tick its box in the plan's TODO list and note anything only the phase PR can settle. Mark the PR ready only when CI is green and every step's review gate (R1/R2/R3, see the plan's review legend) has run and been addressed. Then append `· PR #n` to each of the phase's step lines. The human merges the PR at the end of the phase.
- Never stage `.plans/` or use `git add -f`. Never commit secrets or real env files (`.env`, `*.env`, backups, keys); only `*.example` templates.
- Workflows: never `pull_request_target` or `workflow_run`, never a PR check that needs a secret, never `on.paths` on a required workflow (scope job + always-running gate job, plan §8.4). Flag any `.github/workflows/` change for explicit review.

## Architecture invariants

**Layers.** Views read via `selectors.py` and write via `services.py`, never models directly. Services never import views, forms or the request; they take a **principal** (`SessionPrincipal`, `TokenPrincipal` one operation, `SystemPrincipal` worker without business capability, `OperatorPrincipal` for commands with `--operator <email>`) and check capabilities themselves. Only allowlisted modules construct non-session principals.

**Access** (`apps/access`, plan §3):

- Scoped models only via `visible_to(ctx, capability)` or selectors; no bare `Model.objects` in views, forms, template tags; form choice querysets come from selectors.
- Capabilities from `ROLE_CAPABILITIES`, never mixed across memberships: `ctx.scope_for(capability)`.
- Out of scope → **404**; in scope without capability → **403**; unsupported method → **405**.
- Validity is half-open (`valid_from ≤ now < valid_until`, database clock); inactive companies/branches drop out of every scope.
- Never authorize with `is_superuser` (platform operators only); group admins never modify staff/superusers.
- Scope-defining changes (company/branch `is_active`, `managed_by`, project branches/status) lock `AccessState` `FOR UPDATE`, and changes to a user's authority lock that `User` `FOR NO KEY UPDATE`, before `authorize_in_txn`; database triggers (`access.0002`, `accounts.0003`) bump `org_generation` / `authz_generation`. Memberships and assignments are revoked, never deleted (trigger).
- Views check with `require()` / `@require_capability` / `@mutating` (POST only) or the matching mixins; `{% can %}` only hides UI. Privileged roles need session MFA after `privilege_elevated_at` on every page and in `authorize_in_txn`.
- Every URL **and method** gets rows in `tests/access/test_acceptance_matrix.py`, including redaction and revoked-access cases.
- Master data (catalog, units, suppliers) is installation-wide: models use `InstallationQuerySet`, a grant reaches all of it or none, and what a role sees is decided by the capability (`catalog.view` every role, `supplier.view` only group admins, material managers and warehouse operators; suppliers never reach technicians, partners or site managers outside the expected-deliveries projection). Materials and suppliers are deactivated, never deleted; a material's `reference` is fixed. Imports (`manage import_*`) and `seed_demo` call the same services as the screens, as an `OperatorPrincipal` per capability; a file with any error writes nothing.

**Quantities** (plan §4.5, I1–I12):

- `Decimal(12,3)` quantities, `Decimal(14,4)` prices, never float. Received, pending, draft-reserved, delivered, committed, awaiting-handover, to-purchase and outstanding are **derived**, never stored.
- Transition eligibility uses state-independent predicates only (plan §4.9).
- Pending is a waterfall over open allocations in attribution order, capped by what the line can still receive; draft allocations reserve demand.
- committed = max(D, R) + P + Q ≤ approved. Any change to an input (incl. priority/needed-by) reconciles the whole component: discover, lock L2–L5, verify, apply, fixpoint. Cuts target effective pending. Issuing a draft revalidates.
- Worksite receipts attribute by allocation ceiling; every attribution is capped by A − R (and A − D on site); excess is surplus. R ≤ A and D ≤ A always.
- `allocation.qty` grows only via authorized allocate/increase (rule C5), shrinks only via audited trims ≥ attributed. Receipts split into `ReceiptAttribution` rows.
- Closures record `closed_short_source` (`LINE`/`REQUEST`); request reopen clears only `REQUEST`.
- Composite FK `(material_id, unit_id) → Material(id, unit_id)`; never change a referenced material's unit.

**Writes** (plan §4.7–4.8). Each mutating service, in order: `transaction.atomic()` + `lock_timeout` → `authorize_in_txn(principal, …)` (full check every time, never skipped on unchanged generations) → `IdempotencyRecord` (signed key, 24 h, bound to actor + operation + payload hash; replay re-authorizes; same key + other payload → 409) → locks in canonical order (request headers → request lines → PO headers → their outbox rows → PO lines → receipts → handovers → `StorageQuota` → `DocumentSequence`; header before children; discover → lock → verify) → recompute aggregates → validate invariants and C1–C10 → write → reconcile → `audit.record` + `events.publish`. Retry deadlocks only through the shared wrapper.

- Editing by state: plan §4.4. Material/unit/quantity change only in `DRAFT`; back to `DRAFT` clears decisions. Issued POs change only via audited amendments; snapshots never rewritten.
- Posted receipts/handovers are immutable: void (with reason) and re-enter; a void can reopen documents. `DIRECT_RECEIPT` handovers are voided only through their receipt.
- State changes only via services checked against `ALLOWED_TRANSITIONS`; no FSM library.

**Identifiers.** UUIDv7 primary keys; users see `DocumentSequence` numbers. Audit table is append-only (trigger). Apps are `material_requests` and `handovers`.

## Security

- **Browser:** strict CSP (no inline script/style/handlers); htmx via `<meta name="htmx-config">` with `historyCacheSize: 0`, CSRF in `hx-headers` on `<body>`; authenticated responses `Cache-Control: no-store, private`; pre-auth pages also `no-store`; a URL that carries a token is `Referrer-Policy: no-referrer` and redirects to a token-less URL (pages with a form stay `same-origin`: `no-referrer` makes browsers send `Origin: null` on POST, which CSRF refuses).
- **Banned** outside the `scripts/check_access_rules.py` allowlist: `|safe`, `mark_safe`, `csrf_exempt` (only the upload entry point), `.raw()`, `.extra()`. Mutating views are POST-only; GET/HEAD never call a service. The script also parses the code for the plan §3.6 guardrails: scoped managers in views/forms/template tags, `is_superuser` outside `apps/access`, a public service that writes without reaching `authorize_in_txn` (a helper that writes for an authorized caller is private, `_name`), a service reached from a non-POST-only view, a plain `ModelAdmin`, token/system/operator principals outside `CONSTRUCTOR_MODULES`, SMTP or `send_mail` outside the outbox worker. Only GET, HEAD and POST reach a view (`AllowedMethodsMiddleware`, 405 otherwise).
- **Admin:** every `ModelAdmin` on the service-backed base; access and document models read-only; no deletes.
- **Exposure:** prices only with `price.view`; partners never see prices, suppliers or others' requests; site managers get only expected-deliveries fields (plan §3.4).
- **Auth:** django-allauth ≥ 65.20.1 (CI floor), admin login through allauth; allauth email views off (admin-initiated email change: new address confirms, old can cancel within 24 h, authority re-checked); reset never logs in, no login-by-code; `__Host-` cookies; reset tokens re-validated under the user-row lock (single use, 1 h); "MFA required" = this session passed MFA; MFA secrets encrypted with `MFA_ENCRYPTION_KEYS`; rate limits on `SecurityDatabaseCache` (429 when full); `ALLAUTH_TRUSTED_PROXY_COUNT=1`.
- **Accounts:** offboarding revokes the user's pending invitations and email changes and those they initiated; token redemption locks target and initiator users (id order) then the token, re-checks authority, never changes an active account's credentials; tokens 32 random bytes, hash stored, constant-time compare; first admin only via `manage bootstrap_admin`; `createsuperuser` disabled.
- **Logging:** no secrets, tokens, passwords or needless personal data; token URLs, query strings, `Referer` and `HX-Current-URL` redacted at every layer (sentinel test).
- **Email:** never SMTP in a request; all mail via `EmailOutbox` + `worker` (plan §5): `MAILERS["default"]` is the outbox backend (allauth's mail too), business code queues with `apps.notifications.outbox` inside its transaction, only `apps.notifications.worker` sends (`MAILERS["outbound"]`); recipients stored as user ids, eligibility re-checked at claim; claims commit before I/O; dispatches lock their order first (L4 → L4b, the purchasing app registers a `DispatchSource`); PO cancel cancels unsent dispatches in the same transaction and keeps the `PO_CANCELLATION` obligation; outcome updates conditional on attempt id; worker idle while `flags/worker.paused` exists or `OUTBOUND_EMAIL=off`; security mail sealed with `OUTBOX_ENCRYPTION_KEYS`, wiped after send or expiry; `AMBIGUOUS` dispatches never auto-resent. Django forbids `EMAIL_*` settings next to `MAILERS`: the per-operation timeout is `OUTBOX_SMTP_TIMEOUT`.
- **Other:** explicit timeout on every outbound call; production settings fail closed and `manage.py check --deploy` has zero warnings.
- **Containers:** non-root uid 1000, read-only root fs; only Caddy publishes ports; only Caddy and `web` share `edge`; each DB client (`web`, `worker`, `backup`, `migrate`) has its own internal network; DB never published.
- **Backups:** append-only offsite credentials on the VPS; pruning from the maintainer's machine (`--keep-within 90d`); dumps exclude sessions/cache; restore drills offline.
- **Supply chain:** deps via `pyproject.toml` + `uv.lock`; images and Actions pinned by digest/SHA; Trivy without `--ignore-unfixed`; exceptions in `.trivyignore.yaml` with statement and ≤ 90-day expiry, human-approved.

## Stack

Python 3.14 · Django 6.1 (6.2 LTS = B1) · PostgreSQL 18 + psycopg 3 · htmx **2.x** vendored + django-htmx · Tailwind v4 standalone CLI · django-allauth (account + mfa) · Gunicorn `gthread`, WhiteNoise, Caddy · Docker Compose, uv. No Redis, Celery, JS framework, Node tooling or API layer without an approved plan change.

## Commands

Keep current. The Python commands exist since M1.1, the Docker ones since M1.5; the `e2e` one arrives with M8.2.

```bash
uv sync
uv run ruff format . && uv run ruff check .
uv run mypy .
uv run djlint --check templates apps
uv run python scripts/check_access_rules.py
uv run python scripts/check_security_floors.py   # uv.lock vs. security floors
uv run python scripts/check_trivyignore.py       # .trivyignore.yaml entries valid and unexpired
python scripts/verify_release.py --repo mano8/gmat-m8 --sha <sha> [--tag vX.Y.Z]   # M1.7 release gate, read-only (needs `gh`); rehearses docker-publish's checks without a tag
python scripts/release_tools.py notes --version X.Y.Z   # CHANGELOG section a release would use (also: manifest, scan-targets, scan-issue)
uv run python manage.py makemigrations --check --dry-run
uv run pytest            # needs PostgreSQL 18; DB_* env, defaults in config/settings/test.py
uv run pytest --cov --cov-report=json:coverage.json && uv run python scripts/check_coverage_gates.py
uv run python scripts/check_migrations_reversible.py   # scratch database only: migrates to zero and back
uv run pytest -m e2e     # M8.2: Playwright against the compose stack
uv run python manage.py import_catalog|import_suppliers|import_projects <file.csv|xlsx> --operator <email> [--dry-run]   # M3.3; templates in docs/import-templates/
uv run python manage.py seed_demo --operator <email> [--password …]   # M3.4: dev/test settings only; the A/B/C organization, people, projects, ~200 materials, 5 suppliers

# Compose stack (M1.5); docker_compose/gmat_m8/README.md has the rest
cd docker_compose/gmat_m8 && bash init.sh
docker compose up -d --build --wait                  # https://localhost:8443; .env's COMPOSE_FILE adds Mailpit (local mail sink)
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build
docker compose run --rm web manage bootstrap_admin --email you@example.com --company-code A   # first GROUP_ADMIN; prints a one-time link
docker compose -f docker-compose.yml -f docker-compose.dev.yml run --rm web manage seed_demo   # arrives with M3
LIVE_REQUIRED=1 uv run pytest -m live               # smoke tests against the running stack (CADDY_READ_BODY_TIMEOUT=5s in .env)
touch flags/worker.paused                           # the worker claims and sends nothing until removed
docker build -f docker/app/Dockerfile --target runtime -t gmat-m8:local .   # images alone
```

## Layout

`config/` (settings `base`/`dev`/`test`/`prod`/`build`, `env.py` fail-closed helpers, `logging.py` redaction, urls/wsgi/asgi) · `apps/<app>/` (`core`: `BaseModel`, security cache, checks, middleware, admin base classes, `sequences` (`next_number`), `idempotency`, `locking` (L0-L9, `LockOrder`, `run_in_transaction`/`@transactional`, advisory locks), `events`, `fields` (quantity/price presets), `migration_ops` (composite FKs; `on_update="CASCADE"` for a denormalized key children follow), `commands` (`OperatorCommand`; `also_capabilities` + `principal_for` for a command that spans domains), `tabular` (CSV/XLSX reader), `imports` (`ImportCommand`: report, one transaction per file, `--dry-run`), `demo` + `manage seed_demo`, `changes` (`apply_changes`), `db` (`db_now`, `db_clock`), `forms` (`IdempotentForm`, `ValidityForm`, service errors), `responses` (409/400 answers), `http` (`client_ip`, `is_htmx`); `accounts`: user, `Invitation`, `EmailChangeRequest`, allauth/MFA adapters, sessions, admin site, `tokens` (token hashing, the invitation/email-change/bootstrap `TokenPrincipal`s), `services` (invitations, acceptance, offboarding, email changes, `bootstrap_admin`), `selectors`, `revocation` (pending tokens of an account or its initiator), `eligibility` (security-email rules), `housekeeping` (applies due email changes), token pages under `/invite/` and `/account/email-change/`, `manage bootstrap_admin`, `createsuperuser` disabled; `audit`: append-only `AuditEvent`, `recorder.record()`; `access`: `roles` (`Role`, `Capability`, `ROLE_CAPABILITIES`), `context` (`AccessContext`, `scope_for`), `scopes` (`Scope`, `ScopedQuerySet.visible_to`, `InstallationQuerySet` for catalog and supplier master data), `principals` (`CONSTRUCTOR_MODULES`), `authorization` (`authorize_in_txn`), `policy` (privilege, MFA, staff accounts, self-approval), `middleware` (`request.access`), `mixins` (`require`, `@mutating`, `MutatingViewMixin`), `{% can %}`, `admin.CapabilityModelAdmin`, `AccessState`, L0 lock helpers (`generations`); `organizations`: `Company`, `JointVentureMember`, `Branch`, `Membership`, deactivation preconditions, `grants` (membership target rules, privilege elevation), the people/membership/invitation screens under `/org/` (`views`, `forms`, `selectors`); `projects`: `Project`, `ProjectAssignment`, the project screens under `/projects/` (`views`, `forms`, `selectors`), `manage import_projects`; `locations`: `Location`; `catalog`: `MaterialCategory` (two levels, trigger), `UnitOfMeasure` (seeded, read-only), `Material` (`UNIQUE (id, unit_id)` for the C9 composite FKs), `catalog_fold` + trigram search in `selectors` (`search`, `materials`), the screens under `/catalog/` (`/catalog/search/` is the picker fragment, never prices), `manage import_catalog`; `suppliers`: `Supplier`, the screens under `/suppliers/` (`supplier.view` only), `manage import_suppliers`; `material_requests`: `MaterialRequest`, `MaterialRequestLine` (C9 and C3 composite FKs, scope `request_q`), `transitions` (`ALLOWED_TRANSITIONS`), `ledger` (derived A/R/P/Q/D quantities from registered `FlowSource`s that purchasing and handovers add, live/open predicates, *to purchase*, auto close/reopen eligibility, the deactivation provider), `services` (requester side: drafts, lines, submit with number and procurement snapshot, withdraw, cancel; review: `decide_lines`/`decide_line`/`approve_all` with finalisation and the self-approval rule, `return_to_draft`, `reduce_approved_qty`; sourcing: `set_sourcing`, `resolve_non_catalog_line`, `change_schedule`, `close_line_short`/`reopen_line`, `manual_close`/`reopen`, automatic close; `_reconcile` is where M5.1 wires trims and reconciliation), `selectors`/`forms`/`views`/`urls` (the requester's screens under `/requests/`: project → materials → review → submit → status, "My requests"; `submit` also takes `priority`/`needed_by`/`notes`; the reviewer's under the same prefix: `inbox/` (approval inbox with priority, needed-by and project filters), `<id>/examine/` (review page: per-line decisions, approve all, return for changes, the material manager's sourcing, reduce, schedule, close/reopen, link-to-catalog dialog and the audit history; every action is its own POST-only URL that flashes the outcome and redirects back), `data-testid` selectors for the e2e layer), `templates/material_requests/`; `notifications`: `EmailOutbox`, `EmailAttempt`, `backends` (outbox mailer), `outbox` (queue helpers), `crypto` (sealing), `eligibility` (recipient and security rules), `dispatch` (`DispatchSource` for orders), `services` (claim, outcome, abandoned attempts, housekeeping as `system:worker`), `housekeeping` (task registry), `worker` (loop, SMTP dialogue, deadline, heartbeat; `manage.py worker`)) · `templates/` (`layouts/`, `components/`, `partials/`, `allauth/` and `django/forms/` overrides, error pages) · `locale/` (es, en, fr; `.mo` compiled in the image build) · `static/` (`src/app.css` Tailwind entry, `js/`, `vendor/` htmx with its checksum record; built CSS in `dist/` is git-ignored) · `docker/{app,backup,postgres}` (image sources, entrypoint, backup scripts, DB role bootstrap) · `docker_compose/gmat_m8/` (the stack: compose files and overlays incl. `docker-compose.local-mail.yml` (Mailpit), Caddyfile, env templates, `init.sh` (also adds new template keys to existing env files)) · `scripts/` (CI guards; `verify_release.py` and `release_tools.py` for the release pipeline) · `.github/` (workflows `ci`, `compose-smoke`, `docker-publish` (tag push publishes; dispatch builds only), `image-scan` (nightly); Dependabot; workflow changes need maintainer review) · `tests/{unit,access,integration,live,e2e}` (`live` runs against a started stack; `access` is the acceptance matrix, harness in `tests/access/matrix.py`) (shared helpers in `tests/support.py`; the A/B/C fixture (with the sentinel supplier), people, sentinels and `client_for` in `tests/org.py`; import-file helpers in `tests/imports.py`; tokens read back from queued mail in `tests/tokens.py`; a fake SMTP server in `tests/smtp.py`, outbox helpers in `tests/outbox.py`; tests queue mail in the outbox and the worker's mailer is locmem) · `docs/adr/` (decisions) · `docs/security.md` (security baseline) · `docs/release.md`. Settings load only through `config/env.py` helpers; `prod` refuses to start on a missing or placeholder value. Test DB defaults are seeded in `config/settings/test.py` (pytest-django imports settings before any conftest). Mail is configured with Django 6.1's `MAILERS`, not `EMAIL_BACKEND`; absolute links in email use `PUBLIC_BASE_URL` (production: the first `DJANGO_CSRF_TRUSTED_ORIGINS` entry). Every view that calls a service is POST-only on its own URL; its GET page lives on another URL (`check_access_rules.py`).

## Testing

- PostgreSQL only, never SQLite. Markers: default, `integration`, `live`, `e2e`; `--strict-markers`.
- Coverage ≥ 90 % overall; 100 % for `apps/access`, every `services.py` and `selectors.py`.
- Each service: allowed, denied and revoked roles, invariants, integrity rules, transitions; concurrency-sensitive ones add a threaded test in `tests/integration/`.
- Use the shared A/B/C fixture (plan §8.1, `tests/org.py`: fixtures `org`, `people`, `member`; `Sentinel`) with its sentinels; leak assertions are per projection (plan §8.2). A step that adds a supplier, price or quantity model seeds its sentinels there.
- A new URL needs its row in `ROUTES` (`tests/access/test_acceptance_matrix.py`): GET, HEAD and POST for every actor of `tests/access/matrix.py`, one `Route` per object position, and the sentinels its responses may show.

## Conventions

- English code, comments and commits; UI strings via gettext; default language `es`, also `en`, `fr`.
- Mobile-first templates (check at 360 px); page template + `{% partialdef %}` partials; shared pieces are includes under `templates/components/`. After editing templates rebuild the CSS (dev compose runs Tailwind in watch mode), and after adding strings run `makemessages -l es -l en -l fr` and `compilemessages`.
- Migrations expand/contract and reversible (or flagged in PR and release notes); CI checks.
- Windows dev machine, LF endings; `docker/` and `docker_compose/` scripts are bash for Linux.
- Reference repo for CI/compose/tests: `E:\Dev\fa-workspace\fa-auth-m8` (read only).
