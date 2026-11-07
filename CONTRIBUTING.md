# Contributing to gmat-m8

Thanks for your interest. gmat-m8 is a materials application for companies that buy for projects and worksites, built in planned phases (M1, M2, …) by its maintainer. Contributions are welcome, but they have to fit that plan and its rules, so please read this page first.

By taking part you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).

## Before you start

- **Security problems** never go in a public issue or pull request: follow [SECURITY.md](SECURITY.md).
- **Bugs, accessibility problems and ideas**: open an issue with one of the [templates](https://github.com/mano8/gmat-m8/issues/new/choose).
- **Anything beyond a small fix**: open an issue first and wait for the maintainer's answer. Business rules (who may approve, how quantities are counted, what a role may see) are decided in the plan and in [the ADRs](docs/adr/README.md), not in a pull request; a change that invents one will be declined.
- **No new infrastructure** without an approved plan change: no Redis, Celery, JavaScript framework, Node tooling or API layer.

## Development setup

You need Python 3.14, [uv](https://docs.astral.sh/uv/) and PostgreSQL 18 (the tests never use SQLite). The [README](README.md) covers the environment variables and local development; [docker_compose/gmat_m8/README.md](docker_compose/gmat_m8/README.md) covers the full stack.

```bash
uv sync
uvx pre-commit install           # optional: gitleaks and ruff before each commit
uv run python manage.py migrate
uv run python manage.py runserver
```

On Windows, keep LF line endings (`.gitattributes` enforces them); scripts under `docker/` and `docker_compose/` are bash for Linux.

## How the code is organised

Read [CLAUDE.md](CLAUDE.md) before writing code: it is the working agreement for every contributor, human or agent, and lists the architecture invariants. In short:

- Views read through `selectors` and write through `services`, never through models directly.
- Every mutating service authorizes inside its transaction (`authorize_in_txn`), then locks, validates, writes, and records an audit event.
- Scoped data is read only through `visible_to(ctx, capability)` or selectors. Out of scope answers 404, in scope without the capability 403, an unsupported method 405.
- Quantities are `Decimal`, never float, and derived quantities (received, pending, delivered, …) are computed, never stored.
- Templates follow a strict CSP: no inline script, style or event handler. `|safe`, `mark_safe`, `.raw()` and `.extra()` are banned outside an allowlist.

`scripts/check_access_rules.py` checks most of these in CI. The security baseline is in [docs/security.md](docs/security.md).

## Making a change

1. Branch from the latest `main`: `git switch --no-track -c <short-slug> origin/main`.
2. Commit in small steps with [Conventional Commits](https://www.conventionalcommits.org/): `type(scope): summary`, for example `fix(catalog): keep the search term after paging`. Types in use: `feat`, `fix`, `docs`, `test`, `refactor`, `ci`, `deps`, `chore`. Maintainer phase work adds the step id, e.g. `[M4.2]`.
3. Add tests with the change (see below) and run the local gates.
4. Push and open a pull request against `main`. Fill in every section of the template; write "n/a" where one does not apply.

Never commit secrets or real environment files (`.env`, `*.env`, keys, backups); only `*.example` templates. Never use `--no-verify`.

## Local gates

These are the checks CI runs; a pull request is reviewed once they pass.

```bash
uv run ruff format . && uv run ruff check .
uv run mypy .
uv run djlint --check templates apps
uv run python scripts/check_access_rules.py
uv run python scripts/check_security_floors.py
uv run python scripts/check_trivyignore.py
uv run python manage.py makemigrations --check --dry-run
uv run pytest --cov --cov-report=json:coverage.json && uv run python scripts/check_coverage_gates.py
```

## Tests

- PostgreSQL only; markers `integration`, `live` and `e2e` are strict.
- Coverage stays at 90 % or more overall and at 100 % for `apps/access`, every `services` module and every `selectors` module. After a green `test`, CI sends the report to Codecov and Codacy; a pull request from a fork has no token for either and skips them, and it still passes.
- A function stays at a cyclomatic complexity of 10 or less with at most 8 parameters, and a file under 500 lines of code (Codacy's limits; `lizard` prints them).
- A service needs tests for the allowed, denied and revoked roles, its invariants and its state transitions; a concurrency-sensitive one adds a threaded test in `tests/integration/`.
- Use the shared A/B/C organization fixture in `tests/org.py` and its sentinels to prove nothing leaks across companies, branches or projects.
- A new URL gets rows for GET, HEAD and POST and every actor in `tests/access/test_acceptance_matrix.py`.

## Templates, translations and accessibility

- Mobile first: check every page at 360 px and attach a screenshot to the pull request.
- Every UI string goes through gettext. After adding strings run `uv run python manage.py makemessages -l es -l en -l fr` and `compilemessages`; Spanish is the default language.
- Rebuild the CSS after editing templates (the dev compose stack runs Tailwind in watch mode).
- Keep the checklist in [ACCESSIBILITY.md](ACCESSIBILITY.md#for-contributors) true.

## Migrations and dependencies

- Migrations are reversible and follow expand/contract; CI migrates to zero and back. Flag any exception in the pull request.
- Dependencies live in `pyproject.toml` and `uv.lock` (`uv add`, never a hand-edited lock). GitHub Actions and images are pinned by digest or commit SHA. Dependabot proposes updates; the maintainer reviews them.
- Changes under `.github/workflows/` get an explicit maintainer review. Workflows never use `pull_request_target`, `workflow_run`, `on.paths` on a required workflow, or a secret in a pull-request check.

## Review and merge

The maintainer reviews every pull request and merges it once CI is green and the review is settled; contributors never merge, tag or release. Pull requests are merged with a merge commit, so the individual commits stay on `main`.

## License

The project is licensed under the [Apache License 2.0](LICENSE). By submitting a contribution you agree that it is licensed under the same terms (section 5 of the license).
