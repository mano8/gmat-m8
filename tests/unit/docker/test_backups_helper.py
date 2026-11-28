"""The one helper for ``backups/`` (ADR-0033, ``docs/rootless-host.md``), through the scripts that
use it, under a root and a rootless daemon.

Under the fake rootless daemon ``backups/`` is closed to this account and only the fake ``nsenter``
opens it (``conftest.py``): every path below that succeeds read and wrote there through the
helper alone. Each refusal of the helper's target checks, and each name that does not match its
pattern, stops the script before anything changes, and the gates stay as the script found them.
"""

import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.unit.docker.conftest import DOCKERD_PID, ROOT, ROOTLESSKIT_PID, STACK, World

VERSION = "1.1.0"
PREDEPLOY = f"gmat-20261105T000000Z-pre-deploy-{VERSION}"  # what the fake backup names
LATER = "gmat-20261104T060000Z"
OFFSITE = "gmat-20261104T120000Z-offsite"
SNAPSHOT = "4f2a9c1e"
DUMP_TIME = "2026-11-04T06:00:00Z"
MANIFEST = json.dumps(
    {"app_version": VERSION, "created_at": DUMP_TIME, "row_counts": {"core_thing": 3}}
)
RESTORE = ["--operator", "op@test", "--incident", "x", "--confirm", "RESTORE"]
DAEMONS = ["root", "rootless"]


def on(world: World, daemon: str) -> World:
    if daemon == "rootless":
        world.rootless()
    else:
        world.daemon(daemon)
    return world


def tools(world: World) -> list[str]:
    """What the fake nsenter ran: the tool and its arguments, after the namespace options."""
    prefix = f"-U --preserve-credentials -t {DOCKERD_PID} -- "
    for line in world.nsenter_log():
        assert line.startswith(prefix), line
    return [line.removeprefix(prefix) for line in world.nsenter_log()]


def ran(world: World, fragment: str) -> bool:
    return any(fragment in line for line in world.docker_log())


def deployed(world: World) -> World:
    """VERSION deployed over 1.0.0; logs cleared."""
    world.previously_deployed()
    world.release(VERSION)
    result = world.run("deploy.sh", VERSION)
    assert result.returncode == 0, result.stderr
    world.clear_logs()
    return world


def failed_behind_the_gates(world: World) -> World:
    """VERSION's deploy stopped at step 9 with both gates on; its pre-deploy dump is there."""
    world.previously_deployed()
    world.release(VERSION)
    world.fail_on("web verify-internal")
    assert world.run("deploy.sh", VERSION).returncode != 0
    world.recover()
    world.clear_logs()
    world.put_backup(f"{PREDEPLOY}.dump", "PRE-DEPLOY DUMP")
    world.put_backup(f"{PREDEPLOY}.manifest.json", MANIFEST)
    world.put_backup(f"{PREDEPLOY}.media/ab/cd", "attachment")
    assert world.gates() == (True, True)
    return world


def later_dump(world: World) -> None:
    world.put_backup(f"{LATER}.dump", "LATER DUMP")
    world.put_backup(f"{LATER}.manifest.json", MANIFEST)
    world.put_backup(f"{LATER}.media/ab/cd", "attachment")


def fetched_snapshot(world: World, base: str = OFFSITE) -> None:
    """What a restic restore of the snapshot leaves under backups/.offsite (the fake docker
    puts it there when the restore runs)."""
    files = {
        f"{base}.dump": "OFFSITE DUMP",
        f"{base}.manifest.json": MANIFEST,
        f"{base}.media/ab/cd": "attachment",
        # Named like the dump, but not one of its three: the old glob took it along.
        f"{base}-copy.manifest.json": "{}",
    }
    for name, text in files.items():
        path = world.fake / "snapshot" / "backups" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def drill(world: World, *args: str) -> subprocess.CompletedProcess[str]:
    return world.run(
        "restore-drill.sh", "--operator", "op@test",
        "--app-image", f"registry.test/mano8/gmat-m8@sha256:{'a' * 64}",
        "--backup-image", f"registry.test/mano8/gmat-m8-backup@sha256:{'b' * 64}",
        *args,
    )  # fmt: skip


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def read_command(world: World, name: str) -> str:
    """How the helper reads a dump or a manifest: never through a link at its name."""
    return f"/usr/bin/dd if={world.stack}/backups/{name} iflag=nofollow bs=1M status=none"


def helper(world: World, *args: str) -> subprocess.CompletedProcess[str]:
    """``backups_do`` called on its own, as the scripts call it."""
    return subprocess.run(  # noqa: S603 (the copy of ops/common.sh)
        ["bash", "-c", '. ops/common.sh && backups_do "$@"', "helper", *args],  # noqa: S607
        cwd=world.stack,
        env=world.env,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )


# --- each script's path through the helper ------------------------------------------------------


@pytest.mark.parametrize("daemon", DAEMONS)
class TestEachScriptReadsTheDumpsThroughTheHelper:
    def test_restore_predeploy(self, world: World, daemon: str) -> None:
        stopped = failed_behind_the_gates(on(world, daemon))
        result = stopped.run("recover.sh", "restore-predeploy", "--operator", "op@test",
                             "--confirm", "RESTORE")  # fmt: skip
        assert result.returncode == 0, result.stderr
        assert stopped.restored() == "PRE-DEPLOY DUMP"
        assert ran(stopped, f"backups/{PREDEPLOY}.media:/restored-media:ro"), "never skipped"
        used = tools(stopped)
        if daemon == "root":
            assert not used
        else:
            assert read_command(stopped, f"{PREDEPLOY}.dump") in used
            assert any("-name " + f"{PREDEPLOY}.media -printf %y" in line for line in used)

    def test_restore_after_writes(self, world: World, daemon: str) -> None:
        ready = deployed(on(world, daemon))
        later_dump(ready)
        result = ready.run("recover.sh", "restore", LATER, *RESTORE)
        assert result.returncode == 0, result.stderr
        assert f"LOSS WINDOW: everything written since this dump was taken ({DUMP_TIME})" in (
            result.stderr
        )
        assert ready.restored() == "LATER DUMP"
        assert ran(ready, f"backups/{LATER}.media:/restored-media:ro")
        assert ready.deployed()[-1][-1] == "restore"
        if daemon == "rootless":
            manifest = read_command(ready, f"{LATER}.manifest.json")
            assert sum(line == manifest for line in tools(ready)) == 2, "the version and the time"
        info = [line for line in ready.docker_log() if " info --format " in line]
        assert len(info) == 1, "the daemon is asked once per run"

    def test_restore_offsite(self, world: World, daemon: str) -> None:
        on(world, daemon)
        world.release(VERSION)
        fetched_snapshot(world)
        result = world.run(
            "recover.sh", "restore-offsite", "--snapshot", SNAPSHOT, "--version", VERSION,
            "--operator", "op@test", "--incident", "VPS lost", "--confirm", "RESTORE",
        )  # fmt: skip
        assert result.returncode == 0, result.stderr
        assert world.restored() == "OFFSITE DUMP"
        names = world.backup_names()
        assert {f"{OFFSITE}.dump", f"{OFFSITE}.manifest.json", f"{OFFSITE}.media"} <= names
        assert ".offsite" not in names
        assert f"{OFFSITE}-copy.manifest.json" not in names, "only the dump's own three"
        assert ran(world, f"backups/{OFFSITE}.media:/restored-media:ro")
        if daemon == "rootless":
            assert any(line.startswith("/usr/bin/rm -rf -- ") for line in tools(world))

    def test_drill_from_a_directory(self, world: World, daemon: str, tmp_path: Path) -> None:
        on(world, daemon)
        fetched = tmp_path / "fetched by hand"
        (fetched / f"{LATER}.media" / "ab").mkdir(parents=True)
        (fetched / f"{LATER}.dump").write_text("DRILL DUMP")
        (fetched / f"{LATER}.manifest.json").write_text(MANIFEST)
        (fetched / f"{LATER}.media" / "ab" / "cd").write_text("attachment")
        (fetched / f"{LATER}-other.dump").write_text("not this one")
        result = drill(world, "--dump", LATER, "--from-dir", str(fetched))
        assert "For docs/runbooks/drill-log.md" in result.stdout, result.stderr
        assert "PASS  the dump's checksum and contents match its manifest" in result.stdout
        assert "PASS  row counts match the manifest" in result.stdout, "the manifest on stdin"
        assert "PASS  a sample of attachments opens and matches its checksum" in result.stdout
        assert world.restored() == "DRILL DUMP"
        assert (world.fake / "python.in").read_text() == MANIFEST
        names = world.backup_names()
        assert {f"{LATER}.dump", f"{LATER}.manifest.json", f"{LATER}.media"} <= names
        assert f"{LATER}-other.dump" not in names
        if daemon == "rootless":
            owner = f"/usr/bin/chown -R --no-dereference {os.getuid()}:{os.getgid()} -- "
            assert any(line.startswith(owner) for line in tools(world)), "given to the containers"

    def test_drill_from_a_snapshot_and_wiped(self, world: World, daemon: str) -> None:
        on(world, daemon)
        fetched_snapshot(world)
        result = drill(world, "--snapshot", SNAPSHOT, "--wipe")
        assert "For docs/runbooks/drill-log.md" in result.stdout, result.stderr
        assert world.restored() == "OFFSITE DUMP"
        restic = next(line for line in world.docker_log() if "--entrypoint restic" in line)
        assert f"--user {os.getuid()}:{os.getgid()} " in restic, "the backup service's own uid"
        assert not [name for name in world.backup_names() if name.startswith("gmat-")]
        assert ".offsite" not in world.backup_names()


# --- init.sh and the deploy's preflight -------------------------------------------------------


@pytest.fixture
def uninitialised(world: World) -> World:
    shutil.rmtree(world.stack / "backups")
    for name in (".env", "app.env"):
        (world.stack / name).unlink()
    return world


class TestInit:
    def test_under_a_root_daemon_as_before(self, uninitialised: World) -> None:
        result = uninitialised.run("init.sh")
        assert result.returncode == 0, result.stderr
        assert mode(uninitialised.stack / "backups") == 0o700
        assert mode(uninitialised.stack / "flags") == 0o755
        assert not uninitialised.nsenter_log()

    def test_under_a_rootless_daemon_backups_become_the_containers(
        self, uninitialised: World
    ) -> None:
        uninitialised.rootless()
        result = uninitialised.run("init.sh")
        assert result.returncode == 0, result.stderr
        backups = f"{uninitialised.stack}/backups"
        assert tools(uninitialised) == [
            f"/usr/bin/mkdir -p -- {backups}",
            f"/usr/bin/chown -R --no-dereference {os.getuid()}:{os.getgid()} -- {backups}",
            f"/usr/bin/chmod 0700 -- {backups}",
        ]
        assert mode(uninitialised.stack / "flags") == 0o755
        with uninitialised.opened_backups():
            pass  # created, and closed to this account again

    def test_ci_makes_backups_world_writable_on_a_root_daemon(self, uninitialised: World) -> None:
        assert uninitialised.run("init.sh", "--ci").returncode == 0
        assert mode(uninitialised.stack / "backups") == 0o777

    def test_ci_is_refused_on_a_rootless_daemon(self, uninitialised: World) -> None:
        uninitialised.rootless()
        result = uninitialised.run("init.sh", "--ci")
        assert result.returncode != 0
        assert "never makes backups/ world-writable" in result.stderr
        assert not (uninitialised.stack / "backups").exists()

    def test_a_daemon_that_does_not_answer_widens_nothing(self, uninitialised: World) -> None:
        uninitialised.daemon("down")
        result = uninitialised.run("init.sh")
        assert result.returncode == 0, result.stderr
        assert "run init.sh again once the daemon runs" in result.stderr
        assert mode(uninitialised.stack / "backups") == 0o700

    def test_a_daemon_that_does_not_answer_leaves_backups_as_it_is(self, world: World) -> None:
        """On a rootless host backups/ is the containers' by now: deploy may not change its mode,
        so init.sh leaves it to its next run with the daemon up, and to deploy.sh's check."""
        backups = world.stack / "backups"
        backups.chmod(0o750)
        world.daemon("down")
        result = world.run("init.sh")
        assert result.returncode == 0, result.stderr
        assert mode(backups) == 0o750

    def test_ci_needs_a_daemon_that_answers(self, uninitialised: World) -> None:
        """Without it no one can tell a CI runner's root daemon from a rootless host."""
        uninitialised.daemon("down")
        result = uninitialised.run("init.sh", "--ci")
        assert result.returncode != 0
        assert "--ci needs the Docker daemon to answer" in result.stderr
        assert not (uninitialised.stack / "backups").exists()

    def test_a_remapping_daemon_is_refused(self, uninitialised: World) -> None:
        uninitialised.daemon("userns")
        result = uninitialised.run("init.sh")
        assert result.returncode != 0
        assert "userns-remap" in result.stderr


class TestDeployPreflight:
    """Read-only, before the first gate: a backups/ the backup container cannot write would fail
    the pre-deploy dump at step 6 with the site closed."""

    @pytest.fixture
    def ready(self, world: World) -> World:
        world.previously_deployed()
        world.release(VERSION)
        return world

    def refused(self, world: World, message: str) -> None:
        result = world.run("deploy.sh", VERSION)
        assert result.returncode != 0
        assert message in result.stderr
        assert world.gates() == (False, False)
        stack_calls = [line for line in world.docker_log() if " compose " in f" {line}"]
        assert stack_calls in ([], ["-- compose version"]), "only require_tools' version check"
        assert not world.cosign_log(), "refused before the release is even fetched"

    def test_a_rootless_deploy_reads_only_the_directory(self, ready: World) -> None:
        ready.rootless()
        result = ready.run("deploy.sh", VERSION)
        assert result.returncode == 0, result.stderr
        assert tools(ready) == [f"/usr/bin/stat -c %u %a -- {ready.stack}/backups"]

    def test_a_ci_runner_directory_is_accepted(self, ready: World) -> None:
        (ready.stack / "backups").chmod(0o777)
        assert ready.run("deploy.sh", VERSION).returncode == 0

    def test_a_directory_others_can_read(self, ready: World) -> None:
        (ready.stack / "backups").chmod(0o755)
        self.refused(ready, "mode 755")

    def test_no_directory(self, ready: World) -> None:
        (ready.stack / "backups").rmdir()
        self.refused(ready, "backups/ cannot be read: run bash init.sh")

    def test_a_host_that_moved_to_rootless_without_init(self, ready: World) -> None:
        """backups/ is still deploy's: the namespace sees its root, not the containers' uid."""
        ready.rootless()
        ready.point_at(CONTAINER_UID=os.getuid() + 1)
        self.refused(ready, f"backups/ is uid {os.getuid()} mode 700")
        assert "run bash init.sh" in ready.run("deploy.sh", VERSION).stderr

    def test_under_a_root_daemon_another_owner_names_the_fix(self, ready: World) -> None:
        """init.sh never changes an owner under a root daemon: the message cannot send there."""
        ready.point_at(CONTAINER_UID=os.getuid() + 1)
        # The world points CONTAINER_GID at this account's gid (conftest).
        self.refused(ready, f"sudo chown {os.getuid() + 1}:{os.getgid()} backups")
        assert "run bash init.sh" not in ready.run("deploy.sh", VERSION).stderr

    def test_a_flags_directory_the_worker_cannot_search(self, ready: World) -> None:
        (ready.stack / "flags").mkdir(mode=0o700)
        self.refused(ready, "chmod 0755 flags")

    @pytest.mark.parametrize(
        ("daemon", "message"),
        [("userns", "userns-remap"), ("down", "the Docker daemon does not answer")],
    )
    def test_a_daemon_the_scripts_do_not_run_under(
        self, ready: World, daemon: str, message: str
    ) -> None:
        ready.daemon(daemon)
        self.refused(ready, message)


# --- the namespace target ---------------------------------------------------------------------


def _set(path: Path, text: str) -> None:
    path.unlink()
    path.write_text(text)


def _link(path: Path, target: str) -> None:
    path.unlink()
    path.symlink_to(target)


def _no_nsenter(world: World) -> None:
    """A PATH with every tool but nsenter: the fakes, then links to the system's."""
    (world.fake / "bin" / "nsenter").unlink()
    system = world.fake / "system"
    system.mkdir()
    for directory in os.environ["PATH"].split(os.pathsep):
        if not os.path.isdir(directory):
            continue
        for entry in os.scandir(directory):
            # lexists: a dangling link of the system's (a runner has some) is taken once too.
            if entry.name != "nsenter" and not os.path.lexists(system / entry.name):
                (system / entry.name).symlink_to(entry.path)
    world.env["PATH"] = f"{world.fake / 'bin'}{os.pathsep}{system}"


def _docker_host(world: World, value: str | None, context: str = "") -> None:
    if value is None:
        world.env.pop("DOCKER_HOST")
    else:
        world.env["DOCKER_HOST"] = value
    if context:
        (world.fake / "context").write_text(context)


TARGET_REFUSALS: dict[str, tuple[Any, str]] = {
    "no runtime directory": (
        lambda w: w.env.pop("XDG_RUNTIME_DIR"), "XDG_RUNTIME_DIR is not set"),
    "a tcp endpoint": (
        lambda w: _docker_host(w, "tcp://127.0.0.1:2375"), "talks to tcp://127.0.0.1:2375"),
    "the context names another socket": (
        lambda w: _docker_host(w, None, "unix:///var/run/docker.sock"),
        "not the rootless daemon's socket"),
    "no pid file": (lambda w: (w.runtime / "docker.pid").unlink(), "docker.pid is missing"),
    "a pid file that holds no pid": (
        lambda w: _set(w.runtime / "docker.pid", "4242; true\n"), "does not name"),
    "a pid of no process": (lambda w: _set(w.runtime / "docker.pid", "9999\n"), "does not name"),
    "a pid that is not dockerd": (
        lambda w: _link(w.proc / str(DOCKERD_PID) / "exe", "/usr/bin/sleep"), "does not name"),
    "a parent that is not rootlesskit": (
        lambda w: _link(w.proc / str(ROOTLESSKIT_PID) / "exe", "/usr/bin/bash"),
        "was not started by"),
    "no parent": (
        lambda w: _set(w.proc / str(DOCKERD_PID) / "status", "Name:\tdockerd\n"),
        "was not started by"),
    "this shell's own namespace": (
        lambda w: _link(w.proc / str(DOCKERD_PID) / "ns" / "user", "user:[4026531837]"),
        "not in a user namespace of its own"),
    "root is another account": (
        lambda w: _set(w.proc / str(DOCKERD_PID) / "uid_map",
                       f"0 {os.getuid() + 1} 1\n1 100000 65536\n"), "maps ids"),
    "a range outside the subordinate ids": (
        lambda w: _set(w.proc / str(DOCKERD_PID) / "uid_map",
                       f"0 {os.getuid()} 1\n1 200000 65536\n"), "maps ids"),
    "a group range outside the subordinate ids": (
        lambda w: _set(w.proc / str(DOCKERD_PID) / "gid_map",
                       f"0 {os.getgid()} 1\n1 100000 65537\n"), "maps ids"),
    "subordinate ids of another account": (
        lambda w: _set(w.fake / "subuid", "someone-else:100000:65536\n"), "maps ids"),
    "no nsenter": (_no_nsenter, "nsenter is not installed"),
}  # fmt: skip


class TestTheNamespaceTarget:
    """Each check of docs/rootless-host.md ("Entering the namespace") stops the script before it
    enters anything or changes anything; it never falls back to reading as this account."""

    @pytest.fixture
    def rootless(self, world: World) -> World:
        ready = deployed(on(world, "rootless"))
        later_dump(ready)
        ready.clear_logs()
        return ready

    def assert_nothing_changed(self, world: World, result: Any, message: str) -> None:
        assert result.returncode != 0
        assert message in result.stderr
        assert not world.nsenter_log()
        assert world.gates() == (False, False)
        assert not [line for line in world.docker_log() if "pg_restore" in line or " stop " in line]

    @pytest.mark.parametrize("case", list(TARGET_REFUSALS))
    def test_refused(self, rootless: World, case: str) -> None:
        breaks, message = TARGET_REFUSALS[case]
        breaks(rootless)
        result = rootless.run("recover.sh", "restore", LATER, *RESTORE)
        self.assert_nothing_changed(rootless, result, message)

    def test_the_real_proc_refuses_a_process_that_is_not_dockerd(self, world: World) -> None:
        """Unpointed: /proc itself, and this test's own pid in docker.pid."""
        ready = deployed(world)
        later_dump(ready)
        ready.rootless(fake_proc=False)
        (ready.runtime / "docker.pid").write_text(f"{os.getpid()}\n")
        ready.clear_logs()
        result = ready.run("recover.sh", "restore", LATER, *RESTORE)
        self.assert_nothing_changed(ready, result, "does not name")

    def test_the_context_may_name_the_socket(self, rootless: World) -> None:
        _docker_host(rootless, None, f"unix://{rootless.runtime}//docker.sock")
        result = rootless.run("recover.sh", "restore", LATER, *RESTORE)
        assert result.returncode == 0, result.stderr

    def test_a_daemon_binary_replaced_since_it_started(self, rootless: World) -> None:
        _link(rootless.proc / str(DOCKERD_PID) / "exe", "/usr/bin/dockerd (deleted)")
        result = rootless.run("recover.sh", "restore", LATER, *RESTORE)
        assert result.returncode == 0, result.stderr

    def test_the_environment_cannot_say_which_daemon(self, rootless: World) -> None:
        rootless.env.update(DAEMON_KIND="root", NAMESPACE_PID="1")
        result = rootless.run("recover.sh", "restore", LATER, *RESTORE)
        assert result.returncode == 0, result.stderr
        assert tools(rootless), "still through the namespace, checked"


ENTRY_POINTS = ["restore", "restore-predeploy", "restore-offsite", "drill", "init"]


@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_a_refusal_leaves_the_gates_as_the_script_found_them(
    world: World, tmp_path: Path, entry: str
) -> None:
    """No pid file: every script that reaches backups/ stops before it changes anything."""
    on(world, "rootless")
    if entry == "restore-predeploy":
        failed_behind_the_gates(world)
    elif entry == "restore":
        deployed(world)
        later_dump(world)
    found = world.gates()
    (world.runtime / "docker.pid").unlink()
    world.clear_logs()
    command = {
        "restore": ["recover.sh", "restore", LATER, *RESTORE],
        "restore-predeploy": ["recover.sh", "restore-predeploy", "--operator", "o@t",
                              "--confirm", "RESTORE"],
        "restore-offsite": ["recover.sh", "restore-offsite", "--snapshot", SNAPSHOT,
                            "--version", VERSION, *RESTORE],
        "init": ["init.sh"],
    }  # fmt: skip
    if entry == "drill":
        result = drill(world, "--dump", LATER, "--from-dir", str(tmp_path))
    else:
        result = world.run(*command[entry])
    assert result.returncode != 0
    assert "docker.pid is missing" in result.stderr
    assert world.gates() == found
    assert not world.nsenter_log()
    assert not [line for line in world.docker_log() if " stop " in line or " run " in line]


# --- names ------------------------------------------------------------------------------------

BAD_DUMPS = [
    f"{LATER}-$(touch pwned)",
    f"{LATER};touch pwned",
    f"{LATER}-a b",
    f"{LATER}\ntouch pwned",
    "gmat-*",
    # Arabic-Indic digits: the C locale's [0-9] takes in no digit of another script.
    "gmat-\u0662\u0660\u0662\u0666\u0661\u0661\u0660\u0664T060000Z",
    f"{LATER}-{'x' * 41}",
]
BAD_SNAPSHOTS = ["4f2a9c1e;touch pwned", "4f2a9c1e --target /", "latest", "4F2A9C1E", "4f2a9c"]


@pytest.mark.parametrize("daemon", DAEMONS)
class TestNames:
    """A name reaches the helper, a path or a container only after it matched its pattern, so
    shell metacharacters never get that far."""

    def refused(self, world: World, result: Any, found: tuple[bool, bool]) -> None:
        assert result.returncode != 0
        assert "not a dump name" in result.stderr or "not a restic snapshot id" in result.stderr
        assert not world.nsenter_log()
        assert not list(world.stack.rglob("pwned"))
        assert world.gates() == found
        assert not [line for line in world.docker_log() if " run " in line or " stop " in line]

    @pytest.mark.parametrize("name", BAD_DUMPS, ids=range(len(BAD_DUMPS)))
    def test_a_dump_to_restore(self, world: World, daemon: str, name: str) -> None:
        ready = deployed(on(world, daemon))
        result = ready.run("recover.sh", "restore", name, *RESTORE)
        self.refused(ready, result, (False, False))

    @pytest.mark.parametrize("name", BAD_DUMPS, ids=range(len(BAD_DUMPS)))
    def test_a_dump_to_drill(self, world: World, daemon: str, name: str, tmp_path: Path) -> None:
        on(world, daemon)
        result = drill(world, "--dump", name, "--from-dir", str(tmp_path))
        self.refused(world, result, (False, False))
        assert not (world.fake / "lock").exists(), "refused before the lock"

    def test_the_recorded_pre_deploy_dump(self, world: World, daemon: str) -> None:
        stopped = failed_behind_the_gates(on(world, daemon))
        state = stopped.stack / "deploy.state"
        state.write_text(state.read_text() + "DUMP=gmat-$(touch pwned)\n")
        result = stopped.run("recover.sh", "restore-predeploy", "--operator", "o@t",
                             "--confirm", "RESTORE")  # fmt: skip
        self.refused(stopped, result, (True, True))

    @pytest.mark.parametrize("snapshot", BAD_SNAPSHOTS, ids=range(len(BAD_SNAPSHOTS)))
    def test_a_snapshot(self, world: World, daemon: str, snapshot: str) -> None:
        on(world, daemon)
        world.release(VERSION)
        result = world.run("recover.sh", "restore-offsite", "--snapshot", snapshot,
                           "--version", VERSION, *RESTORE)  # fmt: skip
        self.refused(world, result, (False, False))
        result = drill(world, "--snapshot", snapshot)
        self.refused(world, result, (False, False))

    def test_a_fetched_dump_is_never_moved_by_a_name_that_does_not_match(
        self, world: World, daemon: str
    ) -> None:
        on(world, daemon)
        world.release(VERSION)
        world.put_backup(".offsite/backups/gmat-x$(touch pwned).dump", "FORGED")
        result = world.run("recover.sh", "restore-offsite", "--snapshot", SNAPSHOT,
                           "--version", VERSION, *RESTORE)  # fmt: skip
        assert result.returncode != 0
        assert "not a dump name" in result.stderr
        assert not list(world.stack.rglob("pwned"))
        assert not [line for line in tools(world) if "/usr/bin/mv" in line]
        assert world.gates() == (True, True), "the restore had begun: it stops closed"
        assert not ran(world, "pg_restore")


# --- what is not a file or a directory ---------------------------------------------------------


@pytest.mark.parametrize("daemon", DAEMONS)
class TestEntriesOfAnotherType:
    """A link in backups/ is never followed and never read as "not there"."""

    def test_a_dump_that_is_a_link(self, world: World, daemon: str) -> None:
        ready = deployed(on(world, daemon))
        later_dump(ready)
        with ready.opened_backups() as backups:
            (backups / f"{LATER}.dump").unlink()
            (backups / f"{LATER}.dump").symlink_to(ready.stack / ".env")
        result = ready.run("recover.sh", "restore", LATER, *RESTORE)
        assert result.returncode != 0
        assert f"backups/{LATER}.dump is not a plain file" in result.stderr
        assert ready.gates() == (False, False)
        assert not ran(ready, "pg_restore")

    def test_media_that_is_a_link_stops_the_restore(self, world: World, daemon: str) -> None:
        ready = deployed(on(world, daemon))
        later_dump(ready)
        with ready.opened_backups() as backups:
            shutil.rmtree(backups / f"{LATER}.media")
            (backups / f"{LATER}.media").symlink_to(Path.home())
        result = ready.run("recover.sh", "restore", LATER, *RESTORE)
        assert result.returncode != 0
        assert f"backups/{LATER}.media is not a plain directory" in result.stderr
        assert not ran(ready, "/restored-media")
        assert ready.gates() == (True, True)

    def test_a_read_never_follows_a_link(self, world: World, daemon: str) -> None:
        """A link swapped in after ``exists`` answered is not read either (O_NOFOLLOW)."""
        on(world, daemon)
        later_dump(world)
        with world.opened_backups() as backups:
            (backups / f"{LATER}.manifest.json").unlink()
            (backups / f"{LATER}.manifest.json").symlink_to(world.stack / ".env")
        result = helper(world, "read", "manifest", LATER)
        assert result.returncode != 0
        assert "RESTIC_PASSWORD" not in result.stdout

    def test_backups_that_is_a_link(self, world: World, daemon: str, tmp_path: Path) -> None:
        """Its owner and mode would be the link's, and init.sh could not fix them: refused."""
        world.previously_deployed()
        world.release(VERSION)
        on(world, daemon)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir(mode=0o700)
        backups = world.stack / "backups"
        backups.chmod(0o700)
        backups.rmdir()
        backups.symlink_to(elsewhere)
        world.clear_logs()
        for script in ("deploy.sh", "init.sh"):
            result = world.run(script, *([VERSION] if script == "deploy.sh" else []))
            assert result.returncode != 0, script
            assert "backups/ is a symbolic link" in result.stderr, script
        assert world.gates() == (False, False)
        assert not world.nsenter_log()
        assert mode(elsewhere) == 0o700

    def test_a_drill_never_copies_over_what_backups_holds(
        self, world: World, daemon: str, tmp_path: Path
    ) -> None:
        """A link left at a dump's name would be written through: the drill refuses instead."""
        on(world, daemon)
        later_dump(world)
        with world.opened_backups() as backups:
            (backups / f"{LATER}.dump").unlink()
            (backups / f"{LATER}.dump").symlink_to(world.stack / ".env")
        env_before = (world.stack / ".env").read_text(encoding="utf-8")
        fetched = tmp_path / "fetched"
        fetched.mkdir()
        (fetched / f"{LATER}.dump").write_text("DRILL DUMP")
        (fetched / f"{LATER}.manifest.json").write_text(MANIFEST)
        result = drill(world, "--dump", LATER, "--from-dir", str(fetched))
        assert result.returncode != 0
        assert f"backups/ already holds {LATER}" in result.stderr
        assert (world.stack / ".env").read_text(encoding="utf-8") == env_before
        assert world.gates() == (False, False)

    @pytest.mark.parametrize("script", ["recover", "drill"])
    def test_an_offsite_directory_that_is_a_link_is_not_followed(
        self, world: World, daemon: str, tmp_path: Path, script: str
    ) -> None:
        """Planted before the fetch: nothing is taken from where it points."""
        on(world, daemon)
        world.release(VERSION)
        elsewhere = tmp_path / "elsewhere"
        (elsewhere / "backups").mkdir(parents=True)
        for name, text in ((f"{OFFSITE}.dump", "NOT FETCHED"), (f"{OFFSITE}.manifest.json", "{}")):
            (elsewhere / "backups" / name).write_text(text)
        with world.opened_backups() as backups:
            (backups / ".offsite").symlink_to(elsewhere)
        if script == "recover":
            result = world.run("recover.sh", "restore-offsite", "--snapshot", SNAPSHOT,
                               "--version", VERSION, *RESTORE)  # fmt: skip
        else:
            result = drill(world, "--snapshot", SNAPSHOT)
        assert result.returncode != 0
        assert f"snapshot {SNAPSHOT} holds no dump" in result.stderr
        assert sorted(path.name for path in (elsewhere / "backups").iterdir()) == [
            f"{OFFSITE}.dump",
            f"{OFFSITE}.manifest.json",
        ]
        assert not ran(world, "pg_restore")


# --- flags/ -----------------------------------------------------------------------------------


class TestFlags:
    def test_created_searchable_whatever_the_umask(self, world: World) -> None:
        failed_behind_the_gates(world)
        assert mode(world.stack / "flags") == 0o755

    def test_one_the_worker_cannot_search_leaves_the_gates_as_they_were(self, world: World) -> None:
        stopped = failed_behind_the_gates(world)
        (stopped.stack / "flags").chmod(0o700)
        result = stopped.run("recover.sh", "restore-predeploy", "--operator", "o@t",
                             "--confirm", "RESTORE")  # fmt: skip
        assert result.returncode != 0
        assert "chmod 0755 flags" in result.stderr
        assert stopped.gates() == (True, True)
        assert mode(stopped.stack / "flags") == 0o700, "refused, never widened"
        assert not ran(stopped, "pg_restore")


# --- the scripts as written -------------------------------------------------------------------

COMMON = (STACK / "ops" / "common.sh").read_text(encoding="utf-8")


def test_the_constants_the_tests_rewrite() -> None:
    for line in (
        "CONTAINER_UID=1000",
        "CONTAINER_GID=1000",
        "PROC_ROOT=/proc",
        "SUBUID_FILE=/etc/subuid",
        "SUBGID_FILE=/etc/subgid",
        'DAEMON_KIND=""',
        'NAMESPACE_PID=""',
    ):
        assert line in COMMON.splitlines(), line


def test_the_dump_pattern_is_backup_sh_naming() -> None:
    backup = (ROOT / "docker" / "backup" / "backup.sh").read_text(encoding="utf-8")
    assert "stamp=$(date -u +%Y%m%dT%H%M%SZ)" in backup
    assert 'base="gmat-${stamp}${label:+-$label}"' in backup
    assert "^[A-Za-z0-9._-]{1,40}$" in backup
    assert "DUMP_PATTERN='^gmat-[0-9]{8}T[0-9]{6}Z(-[A-Za-z0-9._-]{1,40})?$'" in COMMON


def test_every_command_of_the_helper_is_a_tool_named_by_its_path() -> None:
    calls = re.findall(r"_daemon_run (\S+)", COMMON)
    assert calls and all(call.startswith("/usr/bin/") for call in calls), calls
    for call in re.findall(r"-exec (\S+)", COMMON):
        assert call.startswith("/usr/bin/"), call


SCRIPTS = {
    name: (STACK / name).read_text(encoding="utf-8")
    for name in ("deploy.sh", "recover.sh", "restore-drill.sh", "init.sh", "ops/common.sh")
}


@pytest.mark.parametrize("name", list(SCRIPTS))
def test_nothing_is_given_to_this_account(name: str) -> None:
    """The only owner a script ever sets is the containers', and no link is followed to set it."""
    code = [line for line in SCRIPTS[name].splitlines() if not line.lstrip().startswith("#")]
    # A line that runs it (a message that names the command for a person does not).
    runs = re.compile(r"(?:^|_daemon_run |-exec |&& |\|\| )(?:/usr/bin/)?chown\b")
    assert any(runs.search(line.strip()) for line in code) == (name == "ops/common.sh")
    for line in code:
        if runs.search(line.strip()):
            assert '"$CONTAINER_UID:$CONTAINER_GID"' in line and "--no-dereference" in line, line


def test_only_a_ci_runner_opens_backups_to_others() -> None:
    widened = {name: [line for line in text.splitlines() if "0777" in line]
               for name, text in SCRIPTS.items()}  # fmt: skip
    assert widened.pop("init.sh") == ["    chmod 0777 backups"]
    assert not any(widened.values())
