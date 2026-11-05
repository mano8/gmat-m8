# gmat-m8

Materials app for a group of electrical-installation companies and their branches. Technicians request materials for worksites, managers approve, purchasing orders from suppliers, and goods are received at a warehouse or on site and handed over.

It is a modular Django monolith: Django 6.1, HTMX 2.x and PostgreSQL 18, deployed with Docker Compose on one VPS. Decisions are recorded in [docs/adr](docs/adr/README.md).

## Status

Phase M1 (foundation) is in progress. The project scaffold, health endpoints, custom user, CI pipeline and security baseline (authentication, MFA, admin, rate limits, headers, log redaction) exist; images, the compose stack and the UI shell follow in the next steps. The security baseline is described in [docs/security.md](docs/security.md).

## Requirements

- Python 3.14
- [uv](https://docs.astral.sh/uv/)
- PostgreSQL 18 (tests and local development)

## Local development

```bash
uv sync

# PostgreSQL connection (the tests default to gmat / gmat / localhost:5432)
export DB_NAME=gmat DB_USER=gmat DB_PASSWORD=change-me DB_HOST=localhost DB_PORT=5432

uv run python manage.py migrate      # also creates the security cache table
uv run python manage.py runserver
```

`dev` settings use plain HTTP cookie names and derive a development-only MFA key, so no secret is needed locally. Accounts arrive through invitations (M2.6); until then, create a local test user from `manage.py shell`.

Settings modules live in `config/settings/`: `dev` (default for `manage.py`), `test`, `prod` (default for WSGI/ASGI, fails closed when a required variable is missing or weak) and `build` (static files at image build time, no database).

| Variable | Used by | Meaning |
| --- | --- | --- |
| `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT` | all | PostgreSQL connection (port defaults to 5432) |
| `DJANGO_SECRET_KEY` | `prod` (required, at least 50 characters), `dev` (optional) | Signing key |
| `DJANGO_ALLOWED_HOSTS` | `prod` (required, no wildcard), `dev` (optional) | Comma-separated host names |
| `DJANGO_DEBUG` | `dev` | Defaults to on in `dev`; refused in `prod` |
| `DJANGO_HSTS_SECONDS`, `DJANGO_HSTS_INCLUDE_SUBDOMAINS`, `DJANGO_HSTS_PRELOAD` | `prod` | HSTS: each off until the maintainer turns it on (default 0, false, false; exception X3) |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | `prod` (required, `https://` only) | Comma-separated origins, e.g. `https://materials.example.org` |
| `MFA_ENCRYPTION_KEYS` | `prod` (required) | Comma-separated Fernet keys, newest first, separate from the secret key; see [docs/security.md](docs/security.md#mfa) |
| `DJANGO_SESSION_AGE_SECONDS` | all | Session lifetime for ordinary accounts (default 14 days) |
| `DJANGO_PRIVILEGED_SESSION_AGE_SECONDS` | all | Session lifetime for privileged accounts (default 12 hours) |
| `DJANGO_SESSION_IDLE_SECONDS` | all | Inactivity that ends a session of an ordinary account (default 2 hours) |
| `DJANGO_PRIVILEGED_SESSION_IDLE_SECONDS` | all | Inactivity that ends a session of a privileged account (default 30 minutes) |
| `ATTACHMENT_SCAN_SOCKET`, `ATTACHMENT_DECODER_SOCKET` | `prod` | The malware scanner's and the image decoder's sockets (the stack's fixed paths by default; `gmat.E022` refuses them empty) |
| `SECURITY_CACHE_MAX_ENTRIES` | all | Capacity of the rate-limit cache (default 50,000; at capacity new keys get 429) |
| `DJANGO_LOG_LEVEL` | all | Root log level (default `INFO`); logs are JSON lines on stdout |
| `DJANGO_DEFAULT_FROM_EMAIL` | all | Sender address of account emails |

Generate an MFA key with `uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.

## Quality gates

```bash
uv run ruff format --check . && uv run ruff check .
uv run mypy .
uv run djlint --check templates apps
uv run python scripts/check_access_rules.py
uv run python scripts/check_security_floors.py
uv run python scripts/check_trivyignore.py
uv run python manage.py makemigrations --check --dry-run
uv run pytest            # needs PostgreSQL 18
```

Coverage must stay at 90 % or more overall (100 % for `apps/access` and every `services.py` and `selectors.py` once they exist).

## Continuous integration

`.github/workflows/ci.yaml` runs on every pull request and on pushes to `main`. Mark these checks as **required** in the `main` ruleset (the names are the job names):

| Check | What it enforces |
| --- | --- |
| `lint` | ruff format and check, djLint, banned constructs and workflow triggers, `uv lock --check`, security floors, valid and unexpired `.trivyignore.yaml` |
| `typecheck` | mypy (strict, django-stubs) |
| `security` | bandit (medium and above), pip-audit on the locked set, Trivy filesystem scan (HIGH and CRITICAL, no `--ignore-unfixed`) |
| `test` | PostgreSQL 18 service, no missing migrations, pytest with the 90 % coverage floor, 100 % gate for `apps/access`, `services.py` and `selectors.py`, migrations reversible |
| `trivy-image` | builds the app and backup images and gates both with Trivy |
| `secret-scan` | gitleaks over the full history |
| `compose-smoke-gate` | from `compose-smoke.yaml` (M1.5) |

Three more workflows are not PR checks. `e2e.yaml` (M8.2) drives a real browser through the compose stack, on a push to `main`, nightly, by hand and on a pull request labelled `e2e`. `docker-publish.yaml` publishes both images, signed, with a signed release manifest, when the maintainer pushes a `v*` tag (a manual run builds without pushing); it refuses a commit that is not on `main` or lacks green `ci.yaml`, `compose-smoke.yaml` and `e2e.yaml` runs. `image-scan.yaml` scans the released images, Caddy and PostgreSQL every night and keeps one issue up to date. Both are described in [docs/release.md](docs/release.md).

Trivy exceptions live in `.trivyignore.yaml` (empty; the entry format is documented in the file). Each needs a statement and an expiry at most 90 days away, and is added only in a pull request the maintainer approves. Reports of the Trivy and bandit scans are kept as workflow artifacts for 90 days.

## Health endpoints

- `/healthz/`: liveness, answers without touching the database.
- `/readyz/`: readiness, checks the database; answers 503 without details when it is down.

## Contributing

Work is organised in phases: one phase, one branch, one pull request. Start with [CONTRIBUTING.md](CONTRIBUTING.md); [CLAUDE.md](CLAUDE.md) has the working rules and architecture invariants, and the release procedure is in [docs/release.md](docs/release.md).

- [Code of Conduct](CODE_OF_CONDUCT.md)
- [Security policy](SECURITY.md): report vulnerabilities privately, never in a public issue
- [Accessibility](ACCESSIBILITY.md): what the interface supports and how to report a barrier

## License

See [LICENSE](LICENSE).
