# ADR-0025: CI runs the tests in four shards at once; time-dependent tests run alone

- **Status:** Accepted (the maintainer's request, issues #42 and #45; implemented in PRs #43 and #44; recorded for issue #46)
- **Date:** 2026-11-08
- **Governs:** `.github/workflows/ci.yaml`, `tests/CLAUDE.md`, `tests/unit/test_serial_marker.py`

## Context

The `test` job ran about 6,100 tests one after another, on one core of a four-core runner, in about 26 minutes. Most of the suite can run in parallel: pytest-xdist gives each worker its own test database, and no test shares a port or a path. Three things cannot be ignored while doing so:

- **Some tests depend on wall-clock time.** A race test starts its second thread after a fixed head start (150 ms) or sleeps while the first holds its locks; a list, search or dashboard test asserts a time budget. When other workers share the CPU, the order flips or the budget is missed.
- **A `transaction=True` test empties every table when it ends,** the rows the migrations seeded included (units of measure). pytest-django therefore runs those tests last. A test that runs after one of them on the same database finds the seeded rows gone.
- **xdist needs every worker to collect the same tests.** A parametrized value made at import time (`timezone.now()`, `uuid7()`) gives each process a different test id, and the run stops.

## Decision

- **Four `test-shard` jobs at once** (`ci.yaml`), each on its own runner and PostgreSQL (data on tmpfs, no fsync): `access-integration` (`tests/access`, `tests/integration`, `tests/unit/docker`) and `unit` (all of `tests` but exactly those paths) run every test that is not `serial`, with `pytest -n auto --dist load`; `serial` runs the `serial` tests but the volume list, alone, with the two migration checks; `volume` runs `tests/integration/test_list_volume.py`, alone.
- **The required check stays `test`, and it runs no test.** It runs whenever the shards ran (`if: !cancelled()`), fails unless every shard passed (a skipped required check counts as passed, so it never relies on `needs` alone), merges the shards' coverage data and applies the 90 % floor and the coverage gates. `coverage-upload` reads its report, as before.
- **`serial` marks a test that depends on wall-clock time:** the whole module in `tests/integration`, the test elsewhere.
- **`--dist load`, never `worksteal`:** `load` hands each worker the next tests from the front of the list, so every worker runs them in pytest-django's order, transactional last. `worksteal` gives the last worker the transactional tail first and then tests taken from the others.
- **A parametrized value made at import time gets explicit `ids=`.**

## Enforcement

- `tests/unit/config/test_workflows.py`: the four shards, `unit` ignoring exactly what `access-integration` runs, `serial` ignoring exactly the volume file, `--dist load`, each shard leaving its coverage data without the floor, and the `test` gate.
- `tests/unit/test_serial_marker.py`: a module of `tests/integration` that calls `sleep`, `perf_counter` or `monotonic` of `time`, or a test elsewhere that calls `perf_counter`, carries `serial`. It reads the source, so a time dependency it cannot see (a short timeout passed to a library) is the reviewer's to notice.
- xdist stops the run when workers collect different tests.

## Alternatives rejected

- **One job, the parallel pass then the serial one** (PR #43): 11 minutes, the two passes one after the other.
- **`--dist worksteal`:** better balance at the end of a run, but it breaks the order pytest-django relies on (8 failures and 512 errors on the unit tests with 4 workers; none with `load`).
- **A test-splitting plugin** (durations file, automatic shards): a dependency and a file to keep current, for less than a minute against two path lists.
- **Making the time-dependent tests independent of time** (barriers instead of head starts): the right end state for the race tests, but a rewrite of every one of them; `serial` keeps them as reliable as they were.

## Consequences

- The `test` verdict comes 4 to 6 minutes after the shards start, at the cost of four runners per run.
- A new test directory lands in `unit` by itself. A shard that grows slow is rebalanced by moving a path from `unit`'s `--ignore` list to `access-integration`'s list; the workflow test fails if only one side changes.
- Locally, plain `uv run pytest` still runs everything in one process; the two-pass command of `CLAUDE.md` matches CI.
- The working rules for writing tests live in `tests/CLAUDE.md`.
