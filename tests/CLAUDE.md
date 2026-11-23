# Tests run in parallel

CI runs this suite under pytest-xdist in four shards at once (ADR-0025, `docs/adr/0025-ci-test-shards.md`). Every test you write or change keeps these rules; the guard tests named below fail when one is broken, and their message names this ADR.

- **Wall-clock time → `serial`.** A test that depends on timing (a thread's head start, a sleep while another holds locks, a time budget, an elapsed time it asserts) is marked `serial`: in `tests/integration` the whole module (`pytestmark = [pytest.mark.integration, pytest.mark.serial, …]`), elsewhere the test (`@pytest.mark.serial`). Guard: `tests/unit/test_serial_marker.py`. It reads the source (`sleep`, `perf_counter`, `monotonic` of `time`), so a dependency it cannot see is yours to mark.
- **Same ids in every process.** A parametrized value made at import time (`timezone.now()`, `uuid7()`, anything random) gets explicit `ids=`; otherwise the workers collect different tests and the run stops.
- **Nothing shared between workers.** Each worker has its own test database (`test_gmat_gw<n>`); a port, a path or a file outside `tmp_path` is not. The catalogs are compiled before the run, so the i18n fixture never writes a `.mo` file under xdist.
- **Transactional tests run last.** A `transaction=True` test empties every table when it ends, seeded rows included; pytest-django runs those tests after the others, and `--dist load` keeps that order on each worker. Never use `--dist worksteal`, never reorder tests, and never let a test rely on running after a transactional one.
- **Where a new test runs.** A new directory under `tests/` lands in the `unit` shard by itself. To move one into `access-integration`, add its path to that shard's list and to `unit`'s `--ignore` list in `.github/workflows/ci.yaml`; `tests/unit/config/test_workflows.py` fails if only one side changes.
- **Locally:** plain `uv run pytest` runs everything in one process. The command that matches CI is in the root `CLAUDE.md` (Commands).

## Layout of the tests

- Directories `unit`, `access`, `integration`, `live`, `e2e` (its own `tests/e2e/CLAUDE.md`), `rehearsal`.
- `unit/docker/conftest.py` runs the host scripts for real against fake `docker`, `cosign` and `curl`, logging each call with the state of both gates; `rehearsal` (M8.4) runs them against real containers: a throwaway copy of the compose directory, a local registry with vN, vN+1 and broken releases built from the checkout, a stand-in for GitHub's API and a `cosign` on PATH that knows what was "signed" or runs the real one to prove a refusal, a restic REST server pair
- `live` runs against a started stack; `access` is the acceptance matrix, harness in `tests/access/matrix.py`, and `test_project_receiving_writes.py` (M9.3: real receipts posted as every actor at each project position and organization state, the grant changed between page and submit and before a replay, forged places and fields)
- Shared helpers: `tests/support.py`
- the A/B/C fixture (with the sentinel supplier), people, sentinels, the sentinel purchase order (`build_sentinel_order`: price and other-project quantity) and `client_for` in `tests/org.py`
- purchase orders written with the ORM in `tests/orders.py`
- receipts posted through the service with `receive` in `tests/unit/receiving/conftest.py`
- a parent of each type, upload files and `attach` (the three phases through the services) in `tests/unit/attachments/conftest.py`
- the volume dataset of M7d.4 (`build_volume`, about 270k rows in 30 s, rolled back at the end of the module) in `tests/volume.py`
- `tests/clamd.py`: a fake `clamd` on a unix socket; the real one is in `tests/live`
- the ledger checks: `tests/invariants.py` (an independent re-derivation of R, P, Q, D from the rows, compared with the app's; `violations(ctx)` for I1-I13, C4-C10 and the automatic transitions of §4.9; `Watch` for what needs the state before a step: refusals write nothing, I11, C5 and C10), `tests/ledger_world.py` (four approved requests of three projects bought by A-MAD), the state machine `tests/integration/test_ledger_machine.py` and the checker's own test `tests/integration/test_invariant_checker.py`
- import-file helpers in `tests/imports.py`
- tokens read back from queued mail in `tests/tokens.py`
- a fake SMTP server in `tests/smtp.py`, outbox helpers in `tests/outbox.py` (`FakeOrders`, `no_dispatch_source` for bare-outbox tests, `make_dispatch`, `start_worker`)
- tests queue mail in the outbox and the worker's mailer is locmem
- Rehearsal: `REHEARSAL_REQUIRED=1 uv run pytest tests/rehearsal -m rehearsal -s` runs `deploy.sh`, `recover.sh`, the drill and the offsite rules against real containers in throwaway projects; about 21 minutes, about 3 GB free at its peak; `REHEARSAL_KEEP=1` leaves them, `REHEARSAL_DIR` chooses where. Skips without `REHEARSAL_REQUIRED=1`.
