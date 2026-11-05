"""``deploy.sh`` and ``recover.sh`` against fakes: the order of a deploy, what stops it, and that
both gates stay on until the commit point (plan §9, F18, F23, F43). Every container operation
is faked; the same scripts against real containers are ``tests/rehearsal``.
"""

import fcntl
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.unit.docker.conftest import (
    APP_DIGEST,
    BACKUP_DIGEST,
    OLD_APP,
    OLD_BACKUP,
    PREFIX,
    REPOSITORY,
    ROOT,
    STACK,
    World,
)

VERSION = "1.1.0"
DUMP = f"gmat-20261105T000000Z-pre-deploy-{VERSION}"
IDENTITY = (
    f"https://github.com/{REPOSITORY}/.github/workflows/docker-publish.yaml@refs/tags/v{VERSION}"
)


def position(log: list[str], fragment: str) -> int:
    for index, line in enumerate(log):
        if fragment in line:
            return index
    raise AssertionError(f"{fragment!r} never ran:\n" + "\n".join(log))


def in_order(log: list[str], *fragments: str) -> None:
    positions = [position(log, fragment) for fragment in fragments]
    assert positions == sorted(positions), dict(zip(fragments, positions, strict=True))


@pytest.fixture
def ready(world: World) -> World:
    world.previously_deployed()
    world.release(VERSION)
    return world


def failed_deploy(world: World, pattern: str) -> subprocess.CompletedProcess[str]:
    world.fail_on(pattern)
    result = world.run("deploy.sh", VERSION)
    assert result.returncode != 0, result.stderr
    world.recover()
    world.clear_logs()
    return result


class TestASuccessfulDeploy:
    def test_runs_the_steps_of_the_plan_in_order(self, ready: World) -> None:
        result = ready.run("deploy.sh", VERSION)
        assert result.returncode == 0, result.stderr
        log = ready.docker_log()
        in_order(
            log,
            f"pull --quiet {PREFIX}/gmat-m8@",
            f"pull --quiet {PREFIX}/gmat-m8-backup@",
            "web migrate-plan",
            "web check",
            "up -d --no-deps caddy",
            "stop web worker",
            "backup run --wait 1800 --label pre-deploy-1.1.0",
            "stop backup",
            "run --rm --no-deps -T migrate",
            "up -d --no-deps --no-build --wait --wait-timeout 300 web worker",
            "web verify-internal",
            "WORKER_EXPECT_STATE=paused",
            "WORKER_EXPECT_STATE=running,outbound-off",
        )

    def test_a_changed_caddyfile_applies_behind_the_gates(self, ready: World) -> None:
        ready.run("deploy.sh", VERSION)
        log = ready.docker_log()
        in_order(log, "stop web worker", "restart caddy", "backup run --wait")
        assert log[position(log, "restart caddy")].startswith("MW ")

    def test_records_the_version_both_digests_and_the_dump(self, ready: World) -> None:
        ready.run("deploy.sh", VERSION)
        row = ready.deployed()[-1]
        assert row[1:] == [VERSION, APP_DIGEST, BACKUP_DIGEST, DUMP, "deploy"]
        assert ready.env_value("APP_DIGEST") == APP_DIGEST
        assert ready.env_value("BACKUP_DIGEST") == BACKUP_DIGEST
        assert ready.env_value("APP_VERSION") == VERSION
        assert ready.state("STATUS") == "done"

    def test_opens_both_gates_only_at_the_end(self, ready: World) -> None:
        ready.run("deploy.sh", VERSION)
        assert ready.gates() == (False, False)
        log = ready.docker_log()
        for fragment in ("-T migrate", "web verify-internal", "WORKER_EXPECT_STATE=paused"):
            assert log[position(log, fragment)].startswith("MW "), fragment
        for line in log[
            position(log, "stop web worker") : position(log, "WORKER_EXPECT_STATE=paused")
        ]:
            assert line.startswith("MW "), line
        assert log[position(log, "web check")].startswith("-- "), "preflight runs before the gates"

    def test_verifies_every_signature_against_the_workflow_identity(self, ready: World) -> None:
        ready.run("deploy.sh", VERSION)
        blob, app, backup = ready.cosign_log()
        issuer = "--certificate-oidc-issuer https://token.actions.githubusercontent.com"
        assert blob.startswith("verify-blob") and blob.endswith("release-manifest.json")
        assert app.endswith(f"{PREFIX}/gmat-m8@{APP_DIGEST}")
        assert backup.endswith(f"{PREFIX}/gmat-m8-backup@{BACKUP_DIGEST}")
        for line in (blob, app, backup):
            assert f"--certificate-identity {IDENTITY}" in line
            assert issuer in line

    def test_the_token_goes_through_stdin(self, ready: World) -> None:
        ready.run("deploy.sh", VERSION)
        assert "fake-read-token" in (ready.fake / "curl_config").read_text()
        assert not [line for line in ready.docker_log() if "fake-read-token" in line]

    def test_old_digests_stay_in_env_until_the_new_ones_start(self, ready: World) -> None:
        ready.fail_on("run --rm --no-deps -T migrate")
        ready.run("deploy.sh", VERSION)
        assert ready.env_value("APP_DIGEST") == OLD_APP
        assert ready.env_value("BACKUP_DIGEST") == OLD_BACKUP

    def test_a_first_deploy_starts_the_database_itself_and_has_no_previous(
        self, world: World
    ) -> None:
        world.release(VERSION)
        result = world.run("deploy.sh", VERSION)
        assert result.returncode == 0, result.stderr
        assert world.state("PREVIOUS_APP_DIGEST") == ""


class TestRefused:
    @pytest.mark.parametrize(
        "pattern",
        ["verify-blob", f"{PREFIX}/gmat-m8@", f"{PREFIX}/gmat-m8-backup@"],
        ids=["unsigned manifest", "unsigned app image", "unsigned backup image"],
    )
    def test_an_unsigned_release_touches_nothing(self, ready: World, pattern: str) -> None:
        (ready.fake / "cosign_fail").write_text(pattern, encoding="utf-8")
        result = ready.run("deploy.sh", VERSION)
        assert result.returncode != 0
        assert "does not verify: refused" in result.stderr
        assert ready.gates() == (False, False)
        touched = [line for line in ready.docker_log() if " compose " in line]
        assert [line for line in touched if "compose version" not in line] == []
        assert len(ready.deployed()) == 1

    @pytest.mark.parametrize(
        "changes",
        [
            {"version": "9.9.9"},
            {"tag": "v9.9.9"},
            {"repository": "attacker/gmat-m8"},
            {"schema_version": 2},
            {"commit": "not-a-commit"},
            {
                "images": {
                    "app": {
                        "repository": "evil.test/gmat-m8",
                        "digest": APP_DIGEST,
                        "ref": f"evil.test/gmat-m8@{APP_DIGEST}",
                    }
                }
            },
        ],
        ids=["version", "tag", "repository", "schema", "commit", "image host"],
    )
    def test_a_manifest_that_is_not_this_release_is_refused(
        self, ready: World, changes: dict[str, Any]
    ) -> None:
        ready.release(VERSION, **changes)
        result = ready.run("deploy.sh", VERSION)
        assert result.returncode != 0
        assert "does not match this release: refused" in result.stderr
        assert ready.gates() == (False, False)

    def test_a_release_that_cannot_be_fetched(self, ready: World) -> None:
        (ready.fake / "release" / "release.json").unlink()
        result = ready.run("deploy.sh", VERSION)
        assert result.returncode != 0
        assert "could not be fetched" in result.stderr

    def test_a_release_without_its_signature(self, ready: World) -> None:
        release = ready.fake / "release" / "release.json"
        document = json.loads(release.read_text())
        document["assets"] = document["assets"][:1]
        release.write_text(json.dumps(document))
        result = ready.run("deploy.sh", VERSION)
        assert result.returncode != 0
        assert "has no release-manifest.json.sigstore.json" in result.stderr

    def test_a_digest_the_compose_files_would_not_run(self, ready: World) -> None:
        ready.env["FAKE_PREFIX"] = "registry.test/other"
        result = ready.run("deploy.sh", VERSION)
        assert result.returncode != 0
        assert "the compose files do not run" in result.stderr
        assert ready.gates() == (False, False)

    @pytest.mark.parametrize("version", ["", "1.0", "v1.0.0", "1.0.0; rm -rf /"])
    def test_a_version_that_is_not_one(self, ready: World, version: str) -> None:
        args = [version] if version else []
        assert ready.run("deploy.sh", *args).returncode == 64

    def test_an_open_admin_allowlist(self, ready: World) -> None:
        ready._set_env("ADMIN_ALLOWED_CIDRS", "0.0.0.0/0 ::/0")
        result = ready.run("deploy.sh", VERSION)
        assert result.returncode != 0
        assert "open to every network" in result.stderr
        ready.env["DEPLOY_ALLOW_OPEN_ADMIN"] = "1"
        assert ready.run("deploy.sh", VERSION).returncode == 0

    def test_no_offsite_repository(self, ready: World) -> None:
        ready._set_env("RESTIC_REPOSITORY", "")
        result = ready.run("deploy.sh", VERSION)
        assert result.returncode != 0
        assert "offsite copy" in result.stderr
        ready.env["DEPLOY_ALLOW_NO_OFFSITE"] = "1"
        assert ready.run("deploy.sh", VERSION).returncode == 0

    def test_an_offsite_repository_that_does_not_answer_stops_before_the_gates(
        self, ready: World
    ) -> None:
        ready.fail_on("--entrypoint restic backup cat config")
        result = ready.run("deploy.sh", VERSION)
        assert result.returncode != 0
        assert "does not answer" in result.stderr
        assert ready.gates() == (False, False)
        assert "nothing changed" in result.stderr

    def test_a_first_deploy_has_no_repository_yet(self, world: World) -> None:
        world.release(VERSION)
        world.fail_on("--entrypoint restic backup cat config")
        assert world.run("deploy.sh", VERSION).returncode == 0

    def test_env_files_readable_by_others(self, ready: World) -> None:
        (ready.stack / "app.env").chmod(0o644)
        result = ready.run("deploy.sh", VERSION)
        assert result.returncode != 0
        assert "app.env must be mode 0600" in result.stderr

    @pytest.mark.parametrize("missing", [".env", "app.env"])
    def test_a_missing_env_file(self, ready: World, missing: str) -> None:
        (ready.stack / missing).unlink()
        assert ready.run("deploy.sh", VERSION).returncode != 0

    def test_a_tool_that_is_missing(self, ready: World) -> None:
        (ready.fake / "bin" / "cosign").unlink()
        result = ready.run("deploy.sh", VERSION)
        assert result.returncode != 0
        assert "cosign is not installed" in result.stderr

    def test_an_irreversible_release_needs_the_version_typed(self, ready: World) -> None:
        ready.release(VERSION, notes="## Notes\n**Irreversible migration:** drops a column\n")
        result = ready.run("deploy.sh", VERSION)
        assert result.returncode != 0
        assert "irreversible" in result.stderr
        assert ready.gates() == (False, False)
        assert ready.run("deploy.sh", VERSION, "--confirm", "9.9.9").returncode != 0
        assert ready.run("deploy.sh", VERSION, "--confirm", VERSION).returncode == 0
        assert ready.state("IRREVERSIBLE") == "1"

    def test_a_checkout_of_another_commit(self, ready: World) -> None:
        subprocess.run(["git", "init", "-q"], cwd=ready.stack, check=True)  # noqa: S607
        subprocess.run(
            [  # noqa: S607
                "git",
                "-c",
                "user.email=a@b",
                "-c",
                "user.name=a",
                "commit",
                "-q",
                "--allow-empty",
                "-m",
                "x",
            ],
            cwd=ready.stack,
            check=True,
        )
        result = ready.run("deploy.sh", VERSION)
        assert result.returncode != 0
        assert "git switch --detach v1.1.0" in result.stderr
        assert ready.gates() == (False, False)


@pytest.mark.parametrize(
    ("pattern", "step", "row"),
    [
        ("stop web worker", 5, "recover.sh rollback"),
        ("backup run --wait", 6, "recover.sh rollback"),
        ("run --rm --no-deps -T migrate", 7, "recover.sh migrate-back"),
        ("up -d --no-deps --no-build --wait", 8, "recover.sh migrate-back"),
        ("web verify-internal", 9, "recover.sh migrate-back"),
    ],
)
class TestAFailureBehindTheGates:
    def test_stops_with_both_gates_on_and_prints_the_decision_row(
        self, ready: World, pattern: str, step: int, row: str
    ) -> None:
        result = failed_deploy(ready, pattern)
        assert ready.gates() == (True, True)
        assert ready.state("STEP") == str(step)
        assert ready.state("STATUS") == "failed"
        assert row in result.stderr
        assert len(ready.deployed()) == 1, "nothing is recorded as deployed"

    def test_the_worker_is_never_told_to_go_on(
        self, ready: World, pattern: str, step: int, row: str
    ) -> None:
        ready.fail_on(pattern)
        ready.run("deploy.sh", VERSION)
        assert not [line for line in ready.docker_log() if "EXPECT_STATE=running" in line]

    def test_a_second_deploy_is_refused_until_a_human_decides(
        self, ready: World, pattern: str, step: int, row: str
    ) -> None:
        failed_deploy(ready, pattern)
        result = ready.run("deploy.sh", VERSION)
        assert result.returncode != 0
        assert "did not finish" in result.stderr
        assert ready.run("deploy.sh", VERSION, "--resume").returncode == 0
        assert ready.gates() == (False, False)


class TestTheCommitPoint:
    def test_a_worker_that_does_not_resume_closes_the_gates_again(self, ready: World) -> None:
        ready.fail_on("WORKER_EXPECT_STATE=running")
        ready.env["FAST_WAIT"] = "1"
        script = (ready.stack / "ops" / "common.sh").read_text()
        (ready.stack / "ops" / "common.sh").write_text(
            script.replace(
                'wait_worker_state "$RUNNING_STATES" 90', 'wait_worker_state "$RUNNING_STATES" 1'
            )
        )
        result = ready.run("deploy.sh", VERSION)
        assert result.returncode != 0
        assert "the worker did not resume" in result.stderr
        assert ready.gates() == (True, True)

    def test_a_slow_host_gets_its_tries_before_the_gates_close(self, ready: World) -> None:
        """One slow call must not use up the wait: the clock alone does not end it."""
        script = (ready.stack / "ops" / "common.sh").read_text()
        (ready.stack / "ops" / "common.sh").write_text(
            script.replace(
                'wait_worker_state "$RUNNING_STATES" 90', 'wait_worker_state "$RUNNING_STATES" 0'
            )
        )
        ready.fail_on("WORKER_EXPECT_STATE=running")
        result = ready.run("deploy.sh", VERSION)
        assert result.returncode != 0
        tries = [line for line in ready.docker_log() if "EXPECT_STATE=running" in line]
        assert len(tries) == 5

    def test_a_site_that_does_not_answer_closes_the_gates_again(self, ready: World) -> None:
        (ready.fake / "public_status").write_text("502")
        script = (ready.stack / "ops" / "common.sh").read_text()
        (ready.stack / "ops" / "common.sh").write_text(
            script.replace(
                "wait_public_status 200 /healthz/ 60", "wait_public_status 200 /healthz/ 1"
            )
        )
        result = ready.run("deploy.sh", VERSION)
        assert result.returncode != 0
        assert "both gates are on again" in result.stderr
        assert ready.gates() == (True, True)
        assert len(ready.deployed()) == 1
        assert ready.state("STEP") == "10"
        assert "forward-fix first" in result.stderr


class TestStoppedBeforeTheGates:
    def test_nothing_changed_so_nothing_needs_recovering(self, ready: World) -> None:
        failed_deploy(ready, "web check")
        assert ready.gates() == (False, False)
        assert "nothing changed" in ready.run("recover.sh", "status").stdout
        result = ready.run("recover.sh", "rollback")
        assert result.returncode != 0
        assert "nothing changed" in result.stderr
        assert ready.gates() == (False, False)

    def test_the_next_deploy_goes_ahead(self, ready: World) -> None:
        failed_deploy(ready, "web check")
        assert ready.run("deploy.sh", VERSION).returncode == 0


class TestStatus:
    def test_names_the_row_for_each_stop(self, ready: World) -> None:
        failed_deploy(ready, "backup run --wait")
        out = ready.run("recover.sh", "status").stdout
        assert "failed before step 7: ./recover.sh rollback" in out
        assert "maintenance gate:  on" in out
        ready.run("recover.sh", "rollback")
        assert "a defect after the commit point" in ready.run("recover.sh").stdout

    def test_steps_seven_to_nine(self, ready: World) -> None:
        failed_deploy(ready, "web verify-internal")
        out = ready.run("recover.sh", "status").stdout
        assert "failed at steps 7-9" in out
        assert DUMP in out


class TestRollback:
    def test_failed_before_the_migration_starts_the_previous_digests(self, ready: World) -> None:
        failed_deploy(ready, "backup run --wait")
        result = ready.run("recover.sh", "rollback")
        assert result.returncode == 0, result.stderr
        assert ready.gates() == (False, False)
        assert ready.env_value("APP_DIGEST") == OLD_APP
        assert ready.deployed()[-1][1:3] == ["1.0.0", OLD_APP]
        assert ready.deployed()[-1][-1] == "rollback"
        log = ready.docker_log()
        in_order(log, "stop web worker", "web verify-internal", "WORKER_EXPECT_STATE=running")
        assert not [line for line in log if "migrate" in line]

    def test_refused_once_the_migration_may_have_run(self, ready: World) -> None:
        failed_deploy(ready, "web verify-internal")
        result = ready.run("recover.sh", "rollback")
        assert result.returncode != 0
        assert "migrate-back or restore-predeploy" in result.stderr
        assert ready.gates() == (True, True)

    def test_a_first_deploy_has_nothing_to_go_back_to(self, world: World) -> None:
        world.release(VERSION)
        failed_deploy(world, "stop web worker")
        result = world.run("recover.sh", "rollback")
        assert result.returncode != 0
        assert "first deploy" in result.stderr

    def test_nothing_to_do_without_a_failed_deploy(self, ready: World) -> None:
        assert ready.run("recover.sh", "rollback").returncode != 0


class TestMigrateBack:
    def test_goes_back_with_the_new_image_then_starts_the_previous_digests(
        self, ready: World
    ) -> None:
        failed_deploy(ready, "web verify-internal")
        (ready.fake / "head").write_text("core 0002_new\nneighbour 0001_new\nsame 0003_x\n")
        (ready.stack / "deploy.head").write_text("core 0001_x\nsame 0003_x\n")
        result = ready.run("recover.sh", "migrate-back")
        assert result.returncode == 0, result.stderr
        log = ready.docker_log()
        in_order(
            log,
            "migrate manage migrate core 0001_x --noinput",
            "up -d --no-deps --no-build --wait",
            "web verify-internal",
        )
        assert [line for line in log if "manage migrate neighbour zero" in line]
        assert not [line for line in log if "manage migrate same" in line]
        for line in log:
            if "manage migrate" in line:
                assert line.startswith("MW ")
        assert ready.env_value("APP_DIGEST") == OLD_APP
        assert ready.deployed()[-1][-1] == "migrate-back"

    def test_an_irreversible_release_cannot(self, ready: World) -> None:
        ready.release(VERSION, notes="Irreversible migration: yes")
        ready.fail_on("web verify-internal")
        ready.run("deploy.sh", VERSION, "--confirm", VERSION)
        ready.recover()
        result = ready.run("recover.sh", "migrate-back")
        assert result.returncode != 0
        assert "restore-predeploy" in result.stderr

    def test_an_empty_saved_head_never_means_back_to_zero(self, ready: World) -> None:
        failed_deploy(ready, "web verify-internal")
        (ready.fake / "head").write_text("core 0002_new\n")
        (ready.stack / "deploy.head").write_text("")
        result = ready.run("recover.sh", "migrate-back")
        assert result.returncode != 0
        assert "previous head is unknown" in result.stderr
        assert not [line for line in ready.docker_log() if "manage migrate" in line]
        assert ready.gates() == (True, True)

    def test_a_head_that_cannot_be_read_stops_the_deploy_before_it_migrates(
        self, ready: World
    ) -> None:
        ready.fail_on("exec -T db sh -c PGPASSWORD")
        result = ready.run("deploy.sh", VERSION)
        assert result.returncode != 0
        assert not [line for line in ready.docker_log() if "-T migrate" in line]
        assert ready.gates() == (True, True)

    def test_one_that_does_not_go_back_keeps_the_gates_on(self, ready: World) -> None:
        failed_deploy(ready, "web verify-internal")
        (ready.fake / "head").write_text("core 0002_new\n")
        (ready.stack / "deploy.head").write_text("core 0001_x\n")
        ready.fail_on("manage migrate core")
        result = ready.run("recover.sh", "migrate-back")
        assert result.returncode != 0
        assert "cannot go back" in result.stderr
        assert ready.gates() == (True, True)
        assert ready.env_value("APP_DIGEST") == APP_DIGEST


class TestRestorePredeploy:
    @pytest.fixture
    def stopped(self, ready: World) -> World:
        failed_deploy(ready, "web verify-internal")
        (ready.stack / "backups" / f"{DUMP}.dump").write_text("DUMP")
        return ready

    def test_preserves_restores_rotates_reconciles_and_only_then_opens(
        self, stopped: World
    ) -> None:
        old_key = stopped.env_value("DJANGO_SECRET_KEY", "app.env")
        result = stopped.run(
            "recover.sh", "restore-predeploy", "--operator", "op@test", "--confirm", "RESTORE"
        )
        assert result.returncode == 0, result.stderr
        log = stopped.docker_log()
        in_order(
            log,
            f"--entrypoint restore backup --verify {DUMP}.dump",
            "pg_dump -Fc",
            "volume create",
            "cp -a /from/. /to/",
            "-d postgres -v db=",
            "pg_restore",
            "post-restore-reconcile --operator op@test --failed-state -",
            "up -d --no-deps --no-build --wait",
            "web verify-internal",
            "WORKER_EXPECT_STATE=running",
        )
        for line in log[: position(log, "WORKER_EXPECT_STATE=paused")]:
            assert line.startswith("MW "), line
        assert stopped.env_value("DJANGO_SECRET_KEY", "app.env") != old_key
        assert stopped.env_value("APP_DIGEST") == OLD_APP
        assert stopped.deployed()[-1][-1] == "restore-predeploy"
        preserved = next((stopped.stack / "preserved").iterdir())
        assert (preserved / "failed.dump").read_text().strip() == "FAILED-DUMP"
        assert json.loads((preserved / "failed-state.json").read_text())["format_version"] == 1
        assert (preserved / "reconcile-report.json").exists()

    def test_a_dump_that_fails_its_check_is_never_restored(self, stopped: World) -> None:
        stopped.fail_on("--entrypoint restore backup --verify")
        result = stopped.run(
            "recover.sh", "restore-predeploy", "--operator", "op@test", "--confirm", "RESTORE"
        )
        assert result.returncode != 0
        assert not [line for line in stopped.docker_log() if "pg_restore" in line]
        assert stopped.gates() == (True, True)

    def test_a_failed_reconciliation_never_opens(self, stopped: World) -> None:
        stopped.fail_on("post-restore-reconcile")
        result = stopped.run(
            "recover.sh", "restore-predeploy", "--operator", "op@test", "--confirm", "RESTORE"
        )
        assert result.returncode != 0
        assert stopped.gates() == (True, True)
        assert not [line for line in stopped.docker_log() if "verify-internal" in line]

    @pytest.mark.parametrize(
        "args",
        [
            ["--confirm", "RESTORE"],
            ["--operator", "op@test"],
            ["--operator", "op@test", "--confirm", "NO"],
        ],
    )
    def test_needs_an_operator_and_the_word(self, stopped: World, args: list[str]) -> None:
        result = stopped.run("recover.sh", "restore-predeploy", *args)
        assert result.returncode != 0
        assert not [line for line in stopped.docker_log() if "pg_restore" in line]

    def test_a_dump_that_is_gone(self, stopped: World) -> None:
        (stopped.stack / "backups" / f"{DUMP}.dump").unlink()
        result = stopped.run(
            "recover.sh", "restore-predeploy", "--operator", "op@test", "--confirm", "RESTORE"
        )
        assert result.returncode != 0
        assert "is not in backups/" in result.stderr


class TestAReleaseThatFollowsAFailedOne:
    def test_going_back_still_means_where_the_failed_one_started(self, ready: World) -> None:
        (ready.fake / "head").write_text("core 0001_x\n")
        failed_deploy(ready, "web verify-internal")
        assert (ready.stack / "deploy.head").read_text() == "core 0001_x\n\n"
        first_dump = ready.state("DUMP")
        # Migrated further by the failed release; its fix fails too, at the same point.
        (ready.fake / "head").write_text("core 0002_failed\n")
        ready.release("1.1.1")
        ready.fail_on("web verify-internal")
        assert ready.run("deploy.sh", "1.1.1", "--resume").returncode != 0
        ready.recover()
        assert (ready.stack / "deploy.head").read_text() == "core 0001_x\n\n"
        assert ready.state("ORIGINAL_DUMP") == first_dump
        assert ready.state("DUMP") == "gmat-20261105T000000Z-pre-deploy-1.1.1"
        ready.clear_logs()
        (ready.stack / "backups" / f"{first_dump}.dump").write_text("DUMP")
        result = ready.run(
            "recover.sh", "restore-predeploy", "--operator", "op@test", "--confirm", "RESTORE"
        )
        assert result.returncode == 0, result.stderr
        assert [line for line in ready.docker_log() if f"--verify {first_dump}.dump" in line]

    def test_the_saved_head_goes_with_the_commit_point(self, ready: World) -> None:
        assert ready.run("deploy.sh", VERSION).returncode == 0
        assert not (ready.stack / "deploy.head").exists()


class TestRollbackImages:
    def deployed_twice(self, world: World, notes: str) -> World:
        world.release(VERSION, notes=notes)
        assert world.run("deploy.sh", VERSION).returncode == 0
        return world

    def test_when_the_notes_say_the_old_code_works(self, ready: World) -> None:
        self.deployed_twice(ready, "Old code works on the new schema: yes")
        result = ready.run("recover.sh", "rollback-images")
        assert result.returncode == 0, result.stderr
        assert ready.env_value("APP_DIGEST") == OLD_APP
        assert ready.deployed()[-1][-1] == "rollback-images"

    def test_refused_otherwise(self, ready: World) -> None:
        self.deployed_twice(ready, "Nothing about it.")
        result = ready.run("recover.sh", "rollback-images")
        assert result.returncode != 0
        assert "forward-fix or restore" in result.stderr
        assert ready.gates() == (False, False)


class TestRestoreAfterWrites:
    @pytest.fixture
    def dump(self, ready: World) -> str:
        assert ready.run("deploy.sh", VERSION).returncode == 0
        ready.clear_logs()
        backups = ready.stack / "backups"
        (backups / "gmat-1.dump").write_text("DUMP")
        (backups / "gmat-1.manifest.json").write_text(json.dumps({"app_version": VERSION}))
        return "gmat-1"

    def test_states_the_loss_window_keeps_the_incident_and_replays(
        self, ready: World, dump: str
    ) -> None:
        result = ready.run(
            "recover.sh",
            "restore",
            dump,
            "--operator",
            "op@test",
            "--incident",
            "#42 bad data",
            "--confirm",
            "RESTORE",
        )
        assert result.returncode == 0, result.stderr
        assert "LOSS WINDOW" in result.stderr
        incident = (ready.stack / "incident.log").read_text()
        assert "#42 bad data" in incident and "gmat-1" in incident and "op@test" in incident
        preserved = next((ready.stack / "preserved").iterdir())
        assert (preserved / "written-since-deploy.json").exists()
        log = ready.docker_log()
        in_order(log, "pg_dump -Fc", "pg_restore", "--failed-state -", "web verify-internal")
        assert ready.env_value("APP_DIGEST") == APP_DIGEST
        assert ready.deployed()[-1][-1] == "restore"

    def test_without_the_decision_nothing_is_replaced(self, ready: World, dump: str) -> None:
        result = ready.run(
            "recover.sh", "restore", dump, "--operator", "op@test", "--incident", "x"
        )
        assert result.returncode != 0
        assert not [line for line in ready.docker_log() if "pg_restore" in line]
        assert [line for line in ready.docker_log() if "pg_dump -Fc" in line]
        assert ready.gates() == (True, True), "the gates stay on while the human thinks"

    @pytest.mark.parametrize("args", [["--operator", "op@test"], ["--incident", "x"]])
    def test_needs_an_operator_and_an_incident(
        self, ready: World, dump: str, args: list[str]
    ) -> None:
        assert ready.run("recover.sh", "restore", dump, *args).returncode != 0
        assert ready.gates() == (False, False)

    def test_a_database_that_cannot_be_read_still_gets_its_volume_copied(
        self, ready: World, dump: str
    ) -> None:
        result = ready.run(
            "recover.sh",
            "restore",
            dump,
            "--operator",
            "op@test",
            "--incident",
            "x",
            "--db-unreadable",
            "--confirm",
            "RESTORE",
        )
        assert result.returncode == 0, result.stderr
        log = ready.docker_log()
        assert not [line for line in log if "pg_dump" in line]
        in_order(log, "cp -a /from/. /to/", "pg_restore", "--no-failed-state")

    def test_an_older_version_is_verified_as_a_release(self, ready: World, dump: str) -> None:
        backups = ready.stack / "backups"
        (backups / "gmat-1.manifest.json").write_text(json.dumps({"app_version": "unknown"}))
        result = ready.run("recover.sh", "restore", dump, "--operator", "o@t", "--incident", "x")
        assert result.returncode != 0
        assert "pass --version" in result.stderr
        ready.release("0.9.0")
        ready.clear_logs()
        result = ready.run(
            "recover.sh",
            "restore",
            dump,
            "--operator",
            "o@t",
            "--incident",
            "x",
            "--version",
            "0.9.0",
            "--confirm",
            "RESTORE",
        )
        assert result.returncode == 0, result.stderr
        assert [line for line in ready.cosign_log() if "verify-blob" in line]
        assert ready.env_value("APP_VERSION") == "0.9.0"


class TestRestoreOffsite:
    def test_comes_up_with_no_failed_state_and_the_listed_accounts(
        self, world: World, tmp_path: Path
    ) -> None:
        world.release(VERSION)
        gone = tmp_path / "gone.txt"
        gone.write_text("left@test\n\n")
        changed = tmp_path / "changed.txt"
        changed.write_text("moved@test")
        offsite = world.stack / "backups" / ".offsite" / "backups"
        offsite.mkdir(parents=True)
        (offsite / "gmat-9.dump").write_text("DUMP")
        (offsite / "gmat-9.manifest.json").write_text("{}")
        result = world.run(
            "recover.sh", "restore-offsite", "--snapshot", "abc123", "--version", VERSION,
            "--operator", "op@test", "--incident", "VPS lost", "--confirm", "RESTORE",
            "--gap-offboarded", str(gone), "--gap-credentials-changed", str(changed),
        )  # fmt: skip
        assert result.returncode == 0, result.stderr
        log = world.docker_log()
        in_order(
            log,
            "restic restore",
            "--verify gmat-9.dump",
            "pg_restore",
            "--no-failed-state",
            "web verify-internal",
        )
        reconcile = log[position(log, "--no-failed-state")]
        assert "--gap-offboarded left@test" in reconcile
        assert "--gap-credentials-changed moved@test" in reconcile
        assert world.deployed()[-1][-1] == "restore-offsite"
        assert (world.stack / "backups" / "gmat-9.dump").exists()

    def test_a_snapshot_without_a_dump(self, world: World) -> None:
        world.release(VERSION)
        (world.stack / "backups" / ".offsite" / "backups").mkdir(parents=True)
        result = world.run(
            "recover.sh", "restore-offsite", "--snapshot", "abc", "--version", VERSION,
            "--operator", "op@test", "--incident", "x", "--confirm", "RESTORE",
        )  # fmt: skip
        assert result.returncode != 0
        assert "holds no dump" in result.stderr


class TestTheLock:
    def test_a_second_run_waits_for_the_first(self, ready: World) -> None:
        with (ready.fake / "lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            waiting = subprocess.Popen(  # noqa: S603 (a copy of the repository's script)
                [str(ready.stack / "deploy.sh"), VERSION],
                cwd=ready.stack,
                env=ready.env,
                stderr=subprocess.PIPE,
                text=True,
            )
            with pytest.raises(subprocess.TimeoutExpired):
                waiting.wait(timeout=2)
            assert not ready.docker_log(), "nothing happens while another run holds the lock"
            fcntl.flock(lock, fcntl.LOCK_UN)
            _, stderr = waiting.communicate(timeout=60)
        assert waiting.returncode == 0
        assert "holds the lock: waiting" in stderr

    def test_a_recovery_that_waited_reads_how_the_deploy_ended(self, ready: World) -> None:
        with (ready.fake / "lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            waiting = subprocess.Popen(  # noqa: S603 (a copy of the repository's script)
                [str(ready.stack / "recover.sh"), "rollback"],
                cwd=ready.stack,
                env=ready.env,
                stderr=subprocess.PIPE,
                text=True,
            )
            with pytest.raises(subprocess.TimeoutExpired):
                waiting.wait(timeout=2)
            # While it waits, the deploy it was started for succeeds.
            ready.state_of_a_finished_deploy()
            fcntl.flock(lock, fcntl.LOCK_UN)
            _, stderr = waiting.communicate(timeout=60)
        assert waiting.returncode != 0
        assert "no failed deploy is recorded" in stderr
        assert not ready.docker_log()


class TestFiles:
    @pytest.mark.parametrize("name", ["deploy.sh", "recover.sh", "restore-drill.sh"])
    def test_scripts_are_executable_and_parse(self, name: str) -> None:
        path = STACK / name
        assert path.stat().st_mode & 0o111
        subprocess.run(["bash", "-n", str(path)], check=True)  # noqa: S603, S607
        assert "set -Eeuo pipefail" in path.read_text(encoding="utf-8")
        assert "umask 077" in path.read_text(encoding="utf-8")

    def test_runtime_files_are_ignored_by_git(self) -> None:
        ignored = (STACK / ".gitignore").read_text(encoding="utf-8").split()
        for name in ("deploy.state", "deploy.head", ".release/", "preserved/", "drill/", "*.log"):
            assert name in ignored

    def test_the_scripts_use_no_secret_on_a_command_line(self) -> None:
        for name in ("deploy.sh", "recover.sh", "restore-drill.sh", "ops/common.sh"):
            text = (STACK / name).read_text(encoding="utf-8")
            assert not re.search(r"PGPASSWORD=\"?\$\(", text)
            assert "--password " not in text, name
            assert "--password-stdin" in text or name != "ops/common.sh"

    def test_an_overlay_cannot_skip_the_signature_checks(self) -> None:
        common = (STACK / "ops" / "common.sh").read_text(encoding="utf-8")
        for flag in (
            "--insecure-ignore-tlog",
            "--insecure-ignore-sct",
            "--allow-insecure-registry",
        ):
            assert flag not in common
        assert not re.search(r"(SKIP|NO)_(VERIFY|SIGNATURE|COSIGN)", common)


def test_the_entrypoint_commands_are_available_in_the_app_image() -> None:
    text = (ROOT / "docker" / "app" / "entrypoint.sh").read_text(encoding="utf-8")
    assert "not_yet" not in text
    assert "manage.py verify_internal" in text
    assert "manage.py post_restore_reconcile" in text
