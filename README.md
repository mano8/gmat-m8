# gmat-m8

Materials app for a group of electrical-installation companies and their branches. Technicians request materials for worksites, managers approve, purchasing orders from suppliers, and goods are received at a warehouse or on site and handed over.

It is a modular Django monolith: Django 6.1, HTMX 2.x and PostgreSQL 18, deployed with Docker Compose on one VPS. Decisions are recorded in [docs/adr](docs/adr/README.md).

## Status

Phase M1 (foundation) is in progress. The project scaffold, health endpoints and custom user exist; CI, the security baseline, images, the compose stack and the UI shell follow in the next steps.

## Requirements

- Python 3.14
- [uv](https://docs.astral.sh/uv/)
- PostgreSQL 18 (tests and local development)

## Local development

```bash
uv sync

# PostgreSQL connection (the tests default to gmat / gmat / localhost:5432)
export DB_NAME=gmat DB_USER=gmat DB_PASSWORD=change-me DB_HOST=localhost DB_PORT=5432

uv run python manage.py migrate
uv run python manage.py runserver
```

Settings modules live in `config/settings/`: `dev` (default for `manage.py`), `test`, `prod` (default for WSGI/ASGI, fails closed when a required variable is missing or weak) and `build` (static files at image build time, no database).

| Variable | Used by | Meaning |
| --- | --- | --- |
| `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT` | all | PostgreSQL connection (port defaults to 5432) |
| `DJANGO_SECRET_KEY` | `prod` (required, at least 50 characters), `dev` (optional) | Signing key |
| `DJANGO_ALLOWED_HOSTS` | `prod` (required, no wildcard), `dev` (optional) | Comma-separated host names |
| `DJANGO_DEBUG` | `dev` | Defaults to on in `dev`; refused in `prod` |
| `DJANGO_HSTS_SECONDS`, `DJANGO_HSTS_PRELOAD` | `prod` | HSTS lifetime (default one year) and preload flag (default on) |

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
| `trivy-image` | Trivy over the app and backup images (placeholder until M1.4) |
| `secret-scan` | gitleaks over the full history |
| `compose-smoke-gate` | from `compose-smoke.yaml` (M1.5) |

Trivy exceptions live in `.trivyignore.yaml` (empty; the entry format is documented in the file). Each needs a statement and an expiry at most 90 days away, and is added only in a pull request the maintainer approves. Reports of the Trivy and bandit scans are kept as workflow artifacts for 90 days.

## Health endpoints

- `/healthz/`: liveness, answers without touching the database.
- `/readyz/`: readiness, checks the database; answers 503 without details when it is down.

## Contributing

Work is organised in phases: one phase, one branch, one pull request. Read [CLAUDE.md](CLAUDE.md) for the working rules and architecture invariants. The release procedure is in [docs/release.md](docs/release.md).

## License

See [LICENSE](LICENSE).
