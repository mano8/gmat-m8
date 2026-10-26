# gmat-m8

Materials app for a group of electrical-installation companies and their branches: technicians request materials for worksites, managers approve, purchasing orders from suppliers, goods are received at a warehouse or on site and handed over. Modular Django monolith (Django 6.1 + HTMX + PostgreSQL 18), Docker Compose on one VPS.

## Source of truth

- **Plans:** markdown files in `.plans/todo/`; there can be several. Work from the plan the maintainer names; "plan §n" in this file means a section of that plan. Plans are local and git-ignored (missing in clones, CI and cloud sessions). If the named plan is missing, or none is named and the choice is unclear, stop and ask; never work from memory of a plan. Before a phase, read the plan's working rules, the phase's step cards and the sections they list.
- **Decisions:** the plan's decisions and answered questions; committed ones live in `docs/adr/` (from M1.1 on), and an ADR wins over any plan. For a question still open, use its working assumption and say so in the PR. Do not invent business rules.
- Keep this file current when commands, layout or rules change.

## Git, PRs, releases

- **One phase = one branch = one PR** (M1, M2, …). Branch: `git switch --no-track -c m<phase>-<slug> origin/main` (e.g. `m2-org-access`). Human steps (`H*`) are skipped. Commit per step with Conventional Commits + step id: `feat(access): resolve dependent branch scope [M2.3]`. Never `--no-verify`.
- Agents use the configured owner account (`gh`/git): commit, `git push -u origin <branch>`, open the PR with `gh pr create --repo mano8/gmat-m8 --head <branch> --draft`, answer reviews.
- **Never:** merge or approve a PR; push to `main` or force-push; create tags or GitHub Releases (`v*` tags publish images); run `gh workflow run` or `docker-publish`; deploy or touch production data; change repo settings, rulesets, secrets or tokens. GitHub will not stop the owner account, so these rules and `.claude/settings.json` are the guard.
- **No attribution:** no `Co-Authored-By` lines, no "Generated with" footers and no AI tool or model names in commits, PR titles, descriptions or comments.
- Mark the PR ready only when CI is green and every step's review gate (R1/R2/R3, see the plan's review legend) has run and been addressed. Then append `· PR #n` to each of the phase's step lines in the plan's TODO list; never tick boxes (the human does after merging).
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
- Every URL **and method** gets rows in `tests/access/test_acceptance_matrix.py`, including redaction and revoked-access cases.

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

- **Browser:** strict CSP (no inline script/style/handlers); htmx via `<meta name="htmx-config">` with `historyCacheSize: 0`, CSRF in `hx-headers` on `<body>`; authenticated responses `Cache-Control: no-store, private`; token and pre-auth pages also `Referrer-Policy: no-referrer`; token URLs redirect to a token-less URL.
- **Banned** outside the `scripts/check_access_rules.py` allowlist: `|safe`, `mark_safe`, `csrf_exempt` (only the upload entry point), `.raw()`, `.extra()`. Mutating views are POST-only; GET/HEAD never call a service.
- **Admin:** every `ModelAdmin` on the service-backed base; access and document models read-only; no deletes.
- **Exposure:** prices only with `price.view`; partners never see prices, suppliers or others' requests; site managers get only expected-deliveries fields (plan §3.4).
- **Auth:** django-allauth ≥ 65.20.1 (CI floor), admin login through allauth; allauth email views off (admin-initiated email change: new address confirms, old can cancel within 24 h, authority re-checked); reset never logs in, no login-by-code; `__Host-` cookies; reset tokens re-validated under the user-row lock (single use, 1 h); "MFA required" = this session passed MFA; MFA secrets encrypted with `MFA_ENCRYPTION_KEYS`; rate limits on `SecurityDatabaseCache` (429 when full); `ALLAUTH_TRUSTED_PROXY_COUNT=1`.
- **Accounts:** offboarding revokes the user's pending invitations and email changes and those they initiated; token redemption locks target and initiator users (id order) then the token, re-checks authority, never changes an active account's credentials; tokens 32 random bytes, hash stored, constant-time compare; first admin only via `manage bootstrap_admin`; `createsuperuser` disabled.
- **Logging:** no secrets, tokens, passwords or needless personal data; token URLs, query strings, `Referer` and `HX-Current-URL` redacted at every layer (sentinel test).
- **Email:** never SMTP in a request; all mail via `EmailOutbox` + `worker` (plan §5): recipients stored as user ids, eligibility re-checked at claim; PO cancel cancels unsent dispatches in the same transaction and keeps the `PO_CANCELLATION` obligation; outcome updates conditional on attempt id; worker idle while `flags/worker.paused` exists or `OUTBOUND_EMAIL=off`; security mail encrypted at rest, wiped after send; `AMBIGUOUS` dispatches never auto-resent.
- **Other:** explicit timeout on every outbound call; production settings fail closed and `manage.py check --deploy` has zero warnings.
- **Containers:** non-root uid 1000, read-only root fs; only Caddy publishes ports; only Caddy and `web` share `edge`; each DB client (`web`, `worker`, `backup`, `migrate`) has its own internal network; DB never published.
- **Backups:** append-only offsite credentials on the VPS; pruning from the maintainer's machine (`--keep-within 90d`); dumps exclude sessions/cache; restore drills offline.
- **Supply chain:** deps via `pyproject.toml` + `uv.lock`; images and Actions pinned by digest/SHA; Trivy without `--ignore-unfixed`; exceptions in `.trivyignore.yaml` with statement and ≤ 90-day expiry, human-approved.

## Stack

Python 3.14 · Django 6.1 (6.2 LTS = B1) · PostgreSQL 18 + psycopg 3 · htmx **2.x** vendored + django-htmx · Tailwind v4 standalone CLI · django-allauth (account + mfa) · Gunicorn `gthread`, WhiteNoise, Caddy · Docker Compose, uv. No Redis, Celery, JS framework, Node tooling or API layer without an approved plan change.

## Commands

Keep current. The Python commands exist since M1.1; the Docker and `e2e` ones arrive with M1.5 and M8.2.

```bash
uv sync
uv run ruff format . && uv run ruff check .
uv run mypy .
uv run djlint --check templates apps
uv run python scripts/check_access_rules.py
uv run python manage.py makemigrations --check --dry-run
uv run pytest            # needs PostgreSQL 18; DB_* env, defaults in config/settings/test.py
uv run pytest -m e2e     # M8.2: Playwright against the compose stack

# M1.5 onwards
cd docker_compose/gmat_m8 && bash init.sh
docker compose up -d --build --wait                  # https://localhost:8443
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.dev.yml run --rm web manage seed_demo
```

## Layout

`config/` (settings `base`/`dev`/`test`/`prod`/`build`, `env.py` fail-closed helpers, urls/wsgi/asgi) · `apps/<app>/` (`core`, `accounts` so far) · `templates/` · `scripts/` · `tests/{unit,access,integration,live,e2e}` · `docs/adr/` (decisions) · `docs/release.md`. Settings load only through `config/env.py` helpers; `prod` refuses to start on a missing or placeholder value. Test DB defaults are seeded in `config/settings/test.py` (pytest-django imports settings before any conftest).

## Testing

- PostgreSQL only, never SQLite. Markers: default, `integration`, `live`, `e2e`; `--strict-markers`.
- Coverage ≥ 90 % overall; 100 % for `apps/access`, every `services.py` and `selectors.py`.
- Each service: allowed, denied and revoked roles, invariants, integrity rules, transitions; concurrency-sensitive ones add a threaded test in `tests/integration/`.
- Use the shared A/B/C fixture (plan §8.1) with its sentinels; leak assertions are per projection (plan §8.2).

## Conventions

- English code, comments and commits; UI strings via gettext; default language `es`, also `en`, `fr`.
- Mobile-first templates (check at 360 px); page template + `{% partialdef %}` partials.
- Migrations expand/contract and reversible (or flagged in PR and release notes); CI checks.
- Windows dev machine, LF endings; `docker/` and `docker_compose/` scripts are bash for Linux.
- Reference repo for CI/compose/tests: `E:\Dev\fa-workspace\fa-auth-m8` (read only).
