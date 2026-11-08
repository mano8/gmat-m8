# Tests run in parallel

CI runs this suite under pytest-xdist in four shards at once (ADR-0025, `docs/adr/0025-ci-test-shards.md`). Every test you write or change keeps these rules; the guard tests named below fail when one is broken, and their message names this ADR.

- **Wall-clock time → `serial`.** A test that depends on timing (a thread's head start, a sleep while another holds locks, a time budget, an elapsed time it asserts) is marked `serial`: in `tests/integration` the whole module (`pytestmark = [pytest.mark.integration, pytest.mark.serial, …]`), elsewhere the test (`@pytest.mark.serial`). Guard: `tests/unit/test_serial_marker.py`. It reads the source (`sleep`, `perf_counter`, `monotonic` of `time`), so a dependency it cannot see is yours to mark.
- **Same ids in every process.** A parametrized value made at import time (`timezone.now()`, `uuid7()`, anything random) gets explicit `ids=`; otherwise the workers collect different tests and the run stops.
- **Nothing shared between workers.** Each worker has its own test database (`test_gmat_gw<n>`); a port, a path or a file outside `tmp_path` is not. The catalogs are compiled before the run, so the i18n fixture never writes a `.mo` file under xdist.
- **Transactional tests run last.** A `transaction=True` test empties every table when it ends, seeded rows included; pytest-django runs those tests after the others, and `--dist load` keeps that order on each worker. Never use `--dist worksteal`, never reorder tests, and never let a test rely on running after a transactional one.
- **Where a new test runs.** A new directory under `tests/` lands in the `unit` shard by itself. To move one into `access-integration`, add its path to that shard's list and to `unit`'s `--ignore` list in `.github/workflows/ci.yaml`; `tests/unit/config/test_workflows.py` fails if only one side changes.
- **Locally:** plain `uv run pytest` runs everything in one process. The command that matches CI is in the root `CLAUDE.md` (Commands).
