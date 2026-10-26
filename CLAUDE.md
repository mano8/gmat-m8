# gmat-m8

Web app for a group of electrical-installation companies and their branches. Technicians request materials for worksites; managers approve them; purchasing orders from suppliers; goods are received at a warehouse or directly on site and handed over to the worksite. Modular Django monolith (Django 6.1 + HTMX + PostgreSQL 18), deployed with Docker Compose on one VPS.

## Source of truth

- **Master plan:** `.plans/todo/gmat-m8-master-plan.md`. Before starting a step, read §0 (working rules), the step's card in §11, and the sections the card lists.
  - The plan is **local and git-ignored**. It is shared from the maintainer's working copy into the agent workspace, and does not exist in fresh clones, CI or cloud sessions. If it is missing, stop and ask. Do not work from memory of it.
  - Never stage anything under `.plans/`, and never use `git add -f`.
- **Decisions:** plan §2, plus the answers to plan §10. M1.1 writes them as committed ADRs in `docs/adr/`. Once those exist, an ADR wins over the plan if the two disagree.
- **Open questions:** plan §10. If a question is still open, use its working assumption and say so in the PR. Do not invent business rules.
- The repository is pre-scaffold until M1.1 lands. Update this file whenever a step changes commands, layout or rules.

## Hard rules: git, PRs, releases

- One plan step = one branch = one PR. Branch from `origin/main` as `m<step>-<slug>` (example: `m2.3-access-engine`).
- You work **from a fork**. `origin` is upstream (fetch only; you have the Read role); `agent` is your fork. Push only to `agent`, then open the PR upstream with `gh pr create --repo <owner>/gmat-m8 --head <agent-user>:<branch> --draft`.
- Mark the PR ready only when CI is green and the step's review gate (R1/R2/R3, plan §0.3) has been run and addressed.
- **Never:**
  - merge or approve a PR;
  - push to `main` or force-push a shared branch;
  - create tags (`v*` tags publish images) or GitHub Releases;
  - run `gh workflow run` or the `docker-publish` workflow;
  - deploy, or touch production data;
  - change repo settings, rulesets, secrets or tokens.

  These are human steps, enforced server-side by the Read role and rulesets (plan §0.1).
- Work only with the dedicated agent account's credentials: its SSH key for git and its classic PAT for `gh` (plan H0.2). Never use the maintainer's credentials.
- Never add `pull_request_target` or `workflow_run` triggers, and never make a PR check depend on a secret. Fork PRs run with a read-only token and no secrets.
- Commits use Conventional Commits with the step id, for example `feat(access): resolve dependent branch scope [M2.3]`. Never use `--no-verify`.
- When the PR is ready for review, append its link to the step's line in the local plan (§1), for example `· PR #12`. Do not tick the checkbox: the human ticks it after merging.
- Never commit secrets or real env files (`.env`, `*.env`, backups, keys). Only `*.example` templates go in git.

## Architecture invariants (do not break)

### Layers

- Views read through `selectors.py` and write through `services.py`. A view never writes a model directly.
- Services never import views, forms or the request object. They take a **principal** and check capabilities themselves, so any future interface stays safe.
- There are four principal types (plan §3.6):
  - `SessionPrincipal`: interactive users;
  - `TokenPrincipal`: invitation acceptance, password reset, email confirmation; one operation only;
  - `SystemPrincipal`: the worker; no business capability;
  - `OperatorPrincipal`: management commands, which require `--operator <email>`.

  Only the allowlisted modules may construct the last three.

### Access control

All access logic lives in `apps/access` (plan §3):

- Scoped models are queried only through `visible_to(ctx, capability)` or selectors. Never use a bare `Model.objects` in views, forms or template tags. Every form choice queryset comes from a selector.
- Capabilities come from the `ROLE_CAPABILITIES` matrix and never mix across memberships. Use `ctx.scope_for(capability)`.
- An object outside the user's scope returns **404**. An object in scope without the needed capability returns **403**. Unsupported methods return **405**.
- Membership and assignment validity is half-open (`valid_from ≤ now < valid_until`, database clock). Inactive companies and branches drop out of every scope (plan S8–S9).
- Never authorize with `is_superuser`; it is reserved for platform operators. Group admins can never modify staff or superuser accounts.
- Every new URL **and HTTP method** gets rows in `tests/access/test_acceptance_matrix.py`, including the redaction and revoked-access cases (plan §8.2). CI fails otherwise.

### Quantities

- Quantities are `Decimal(12,3)` and prices `Decimal(14,4)`. Never use float.
- Received, pending, draft-reserved, delivered, committed, awaiting-handover, to-purchase and outstanding quantities are **derived** from documents, never stored (plan §4.5, invariants I1–I12).
- Keep **state-independent predicates** (line has capacity, request line is live) separate from **current-state predicates** (receivable, open). Transition eligibility uses only the first kind (plan §4.9).
- **Pending** is a waterfall: a receivable line's remaining quantity is spread over the allocations of open request lines, in attribution order, so it never exceeds what the line can still receive. **Draft** allocations reserve demand. There is no stored "released" quantity.
- **Commitments**: committed = max(D, R) + P + Q must stay ≤ approved.
  - Pending depends on **other** requests through the waterfall, so reconciliation works on the whole **component** (request lines and order lines linked by allocations).
  - Every operation that changes an input reconciles, including **priority or needed-by changes**.
  - Discover the component, lock all of it at L2–L5, verify it, apply the operation, then run the fixpoint.
  - Cuts target **effective pending** (`qty := attributed + (P − cut)`), never just the ceiling.
  - Issuing a draft revalidates its lines.
- Worksite receipts attribute by allocation **ceiling** for that project, not by waterfall position.
- Every attribution is capped by the request line's **receipt capacity** A − R, counted across all orders (and by A − D at a worksite). The excess is surplus, manual splits obey the same caps, and posting a receipt reconciles. R ≤ A and D ≤ A always hold, whatever the line's state.
- `allocation.qty` **increases** only through an explicit, authorized allocate / increase operation validated by rule C5 (demand and order-line capacity). It **decreases** only through audited **trims** that never go below the attributed quantity.
- Receipts are split into `ReceiptAttribution` rows, never counted against every linked request.
- Line closures record `closed_short_source` (`LINE` / `REQUEST`). A request reopen clears only `REQUEST` closures.
- Units are bound by the composite FK `(material_id, unit_id) → Material(id, unit_id)`. Never change a referenced material's unit.

### Writes

- Every mutating service follows this order (plan §4.7–4.8):
  1. Run inside `transaction.atomic()` with a `lock_timeout`.
  2. Call `authorize_in_txn(principal, …)`. It takes `FOR SHARE` on `AccessState` and the principal's user row, then **always** runs the full check for that principal type. For a session principal:
     - the user is active and the session epoch is current;
     - memberships are valid at the database clock;
     - the capability and object scope hold for this operation;
     - session MFA happened after `privilege_elevated_at`.

     Never skip the check because generations are unchanged.
  3. Insert the `IdempotencyRecord`. The key is signed, valid 24 h, and bound to actor + operation + payload hash.
  4. Lock rows in the canonical order L2–L9:
     - request headers → request lines → purchase-order headers → outbox rows of those orders (L4b) → purchase-order lines → receipt headers → handover headers → `StorageQuota` → `DocumentSequence`;
     - outbox rows tied to an order are always locked **after** the order, by every path (claim, outcome, cancel, amend);
     - a header always comes before its children;
     - if the set to lock comes from a read, use discover → lock → verify.
  5. Recompute the aggregates inside the lock.
  6. Validate the invariants and the cross-document rules C1–C10 (plan §4.6), even when called without a form.
  7. Write.
  8. Run component reconciliation if the operation changes any input of it (plan §4.5).
  9. Call `audit.record(...)` and `events.publish(...)`.
- Retry deadlocks and lock timeouts through the shared wrapper only.
- Idempotent replays re-authorize before returning the existing document. The same key with a different payload returns 409.
- Editing rules by state are in plan §4.4. Material, unit and quantity change only in `DRAFT`; going back to `DRAFT` clears every decision. Approved quantities are reduced only on live lines, never below max(D, R) + P + Q. Issued purchase orders change only through audited amendments; their snapshots are never rewritten from master data.
- Posted receipts and handovers are immutable. Correct them by voiding (with a reason) and entering them again. A void can reopen closed documents (plan §4.9).
- `DIRECT_RECEIPT` handovers are never voided on their own, only through their receipt.
- State changes go only through service functions checked against `ALLOWED_TRANSITIONS`. No FSM library.

### Identifiers and naming

- Primary keys are UUIDv7. Users see document numbers generated by `DocumentSequence`.
- The audit table is append-only (DB trigger). Never update or delete audit rows.
- App names are `material_requests` and `handovers`, not `requests` or `deliveries`.

## Security rules

- **CSP and browser**:
  - strict CSP: no inline scripts, styles or event handlers;
  - htmx is configured through `<meta name="htmx-config">` with `historyCacheSize: 0`, and the CSRF token goes in `hx-headers` on `<body>`;
  - authenticated responses are `Cache-Control: no-store, private`;
  - token-bearing and pre-authentication pages (login, reset, invitation, email confirmation, MFA) also send `Referrer-Policy: no-referrer` + `no-store`, anonymous responses included;
  - token URLs redirect to a token-less URL after the first hit.
- **Banned constructs**: no `|safe`, `mark_safe`, `csrf_exempt`, `.raw()` or `.extra()` outside the allowlist in `scripts/check_access_rules.py`. The only `csrf_exempt` allowed is the upload entry point. It admits the upload in a `ReservingUploadHandler` before the body is parsed, then calls an inner `csrf_protect` view (plan §5 Attachments).
- **Mutating views are POST-only.** GET and HEAD never call a service.
- **Django Admin**: build every `ModelAdmin` on the service-backed base class. Access and document models are read-only there, master-data saves go through services, and nothing can be deleted.
- **Data exposure**: prices are rendered and exported only with the `price.view` capability. Partner users never see prices, suppliers or other users' requests. Site managers get only the expected-deliveries fields (plan §3.4).
- **Authentication**:
  - django-allauth **≥ 65.20.1** (floor checked in CI);
  - the admin login goes through allauth;
  - allauth's email-management and email-confirmation views are disabled. Email changes are administrator actions:
    - confirmed by the new address;
    - cancellable by the old address during a 24 h cooling-off;
    - re-checked against the initiator's current authority at confirmation and at application;
  - a password reset never logs the user in, and login-by-code is off;
  - session and CSRF cookies use the `__Host-` prefix;
  - password-reset tokens are stateless: re-validate them under the user-row lock before setting the password (one redemption only, 1 h lifetime);
  - "MFA required" means the **current session** passed MFA;
  - MFA secrets are encrypted with `MFA_ENCRYPTION_KEYS`;
  - rate limits use `SecurityDatabaseCache`, which has atomic `add` / `incr`, never evicts live entries and answers 429 when full;
  - the client IP comes from `ALLAUTH_TRUSTED_PROXY_COUNT=1`.
- **Logging**: no secrets, tokens, passwords or unnecessary personal data in any log. Token-bearing URLs, query strings, `Referer` and `HX-Current-URL` are redacted at every layer (Caddy filter, Gunicorn access log off, Django filter, error tracking), and the sentinel test proves it.
- **Email**: never send SMTP inside a request. Every email goes through the `EmailOutbox` and the `worker` (plan §5):
  - notifications store recipient **user ids** and an eligibility rule, re-checked when the worker claims the message;
  - a purchase-order cancel cancels its unsent dispatches in the same transaction. A cancellation notice (`PO_CANCELLATION`) has its own eligibility, and the obligation to send one survives an in-flight dispatch;
  - every outcome update is conditional on the attempt id;
  - the worker does nothing while `flags/worker.paused` exists or `OUTBOUND_EMAIL=off`;
  - security emails are encrypted at rest and wiped after sending;
  - an `AMBIGUOUS` supplier dispatch is never resent automatically.
- **Accounts**:
  - Offboarding revokes, in the same transaction, the user's pending invitations and email changes, and those **they initiated** for others.
  - Token redemption locks the target and initiator `User` rows (in id order), then the token row. It re-checks the initiator's authority, and never changes the credentials of an active account.
  - Tokens are 32 random bytes; only their hashes are stored, and they are compared in constant time.
  - The first administrator comes only from `manage bootstrap_admin` (shell, one-time). `createsuperuser` is disabled.
- **Outbound calls**: every other outbound call sets an explicit timeout.
- **Fail closed**: production settings fail closed, and `manage.py check --deploy` must stay at zero warnings.
- **Containers**: run as non-root uid 1000 with a read-only root filesystem. Only Caddy publishes ports. Only Caddy and `web` share the `edge` network. Each database client (`web`, `worker`, `backup`, `migrate`) has its own internal network to `db`, so no other container can reach `web`. The database is never published in production.
- **Backups**:
  - the VPS holds only append-only offsite credentials;
  - pruning runs from the maintainer's machine with `--keep-within 90d`, and refuses to run on forged-looking snapshots;
  - dumps exclude session and cache data;
  - restore drills have no outbound connectivity.
- **Dependencies**: new ones go through `pyproject.toml` + `uv.lock`. Docker base images and GitHub Actions are pinned by digest or SHA.
- **Vulnerabilities**: Trivy runs without `--ignore-unfixed`. An exception goes in `.trivyignore.yaml` with a statement and an expiry of 90 days or less, and only through a PR the human approves.

## Stack

- Python 3.14
- Django 6.1 (upgrade to 6.2 LTS is backlog item B1)
- PostgreSQL 18 with psycopg 3
- htmx **2.x**, vendored (not 4.x), with django-htmx
- Tailwind v4 standalone CLI (no Node)
- django-allauth (account + mfa)
- Gunicorn `gthread`, WhiteNoise, Caddy
- Docker Compose, uv

Do not add Redis, Celery, a JavaScript framework, Node tooling or an API layer unless the human approves a plan change.

## Commands

These are available from M1.1. Keep this section current.

```bash
uv sync                                              # install, dev group included
uv run ruff format . && uv run ruff check .
uv run mypy .
uv run djlint --check templates apps
uv run python scripts/check_access_rules.py
uv run python manage.py makemigrations --check --dry-run
uv run pytest                                        # unit + integration, needs PostgreSQL 18
uv run pytest -m e2e                                 # Playwright, against the compose stack
```

Run the full stack locally:

```bash
cd docker_compose/gmat_m8
bash init.sh                                         # env files + generated secrets
docker compose up -d --build --wait                  # https://localhost:8443
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.dev.yml run --rm web manage seed_demo
```

## Testing rules

- Tests run on PostgreSQL only. Never use SQLite: the design depends on locks, CHECK constraints and triggers.
- Markers: default (unit/service), `integration`, `live`, `e2e`. Run with `--strict-markers`.
- Coverage: the whole project ≥ 90 %; `apps/access`, every `services.py` and every `selectors.py` = 100 %.
- Every service needs tests for allowed roles, denied roles, revoked access, its invariants, its integrity rules and its state transitions. Concurrency-sensitive services also need a threaded test in `tests/integration/`.
- Use the shared fixture (plan §8.1) instead of building ad-hoc organizations. It provides the A/B/C organization, mixed-membership and revoked users, and **sentinel values** for leak checks.
- Leak assertions are **per projection**: each matrix row lists the sentinels it may show (for example, a site manager may see the supplier name), and every other sentinel must be absent (plan §8.2).
- Never filter a required workflow with `on.paths`. Filter inside the workflow with a scope job and an always-running gate job (plan §8.4).

## Conventions

- Code, identifiers, comments and commit messages are in English. UI strings are wrapped in gettext. The default language is Spanish (`es`), with `en` and `fr` also available.
- Templates are mobile-first: check them at 360 px. Each view has a page template plus HTMX partials (`{% partialdef %}`).
- Migrations follow expand/contract. Each one is reversible, or its PR and the release notes say it is not. CI checks reversibility.
- The development machine runs Windows. Use LF line endings. Scripts in `docker/` and `docker_compose/` are bash and must run on Linux.
- Reference for CI, compose and test patterns: `E:\Dev\fa-workspace\fa-auth-m8`. Read it, never modify it.
