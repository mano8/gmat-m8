"""``deploy.sh``'s checks of the host itself (M12.3, ADR-0033): cosign 3 or newer, a rootless
daemon's port driver that keeps the visitor's address, and the ``docker`` group; and what
``recover.sh status`` says of the daemon.

Every check is read-only and runs before the release is fetched: a host set up the wrong way is
refused before anything changes, and the message names the fix. What the real tools print is in
``fixtures/`` (``conftest.py`` says where each comes from).
"""

import ast
import json
import os
import pwd
import subprocess
import sys
from pathlib import Path

import pytest

from tests.unit.docker.conftest import FIXTURES, ROOT, World

VERSION = "1.1.0"
ME = pwd.getpwuid(os.getuid()).pw_name
OVERRIDE = "DOCKERD_ROOTLESS_ROOTLESSKIT_PORT_DRIVER=slirp4netns"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def arguments(name: str) -> list[str]:
    return fixture(name).splitlines()


@pytest.fixture
def ready(world: World) -> World:
    world.previously_deployed()
    world.release(VERSION)
    return world


@pytest.fixture
def rootless(ready: World) -> World:
    ready.rootless()
    return ready


def snapshot(world: World) -> tuple[bytes, list[str]]:
    """The record of the last deploy and the release files fetched so far."""
    state = world.stack / "deploy.state"
    release = world.stack / ".release"
    return (
        state.read_bytes() if state.exists() else b"",
        sorted(str(path) for path in release.rglob("*")) if release.exists() else [],
    )


def refused_before_anything(world: World, *args: str) -> subprocess.CompletedProcess[str]:
    """Refused with the gates as they were, nothing fetched or verified, nothing run in the
    stack, and the record of the last deploy untouched."""
    gates = world.gates()
    before = snapshot(world)
    result = world.run("deploy.sh", VERSION, *args)
    assert result.returncode != 0, result.stderr
    assert world.gates() == gates
    assert not world.cosign_log(), "refused before the release is even fetched"
    flags = ("M" if gates[0] else "-") + ("W" if gates[1] else "-")
    stack_calls = [line for line in world.docker_log() if " compose " in f" {line}"]
    assert stack_calls in ([], [f"{flags} compose version"]), stack_calls
    assert snapshot(world) == before
    return result


def deployed(world: World, *args: str) -> subprocess.CompletedProcess[str]:
    result = world.run("deploy.sh", VERSION, *args)
    assert result.returncode == 0, result.stderr
    assert world.deployed()[-1][1] == VERSION
    return result


# --- cosign ---------------------------------------------------------------------------------------


class TestCosign:
    def test_cosign_3_goes_ahead_and_is_named(self, ready: World) -> None:
        assert "cosign v3.0.6" in deployed(ready).stderr

    @pytest.mark.parametrize(
        ("output", "exit_code", "said"),
        [
            (fixture("cosign-version-v2.5.0.txt"), 0, "this is cosign v2.5.0"),
            ("GitVersion:    devel\n", 0, "no GitVersion"),
            ("", 1, "no GitVersion"),
        ],
        ids=["cosign 2", "unparsable", "fails"],
    )
    def test_anything_else_is_refused_with_the_fix(
        self, ready: World, output: str, exit_code: int, said: str
    ) -> None:
        ready.cosign_version(output, exit_code=exit_code)
        result = refused_before_anything(ready)
        assert "cosign 3 or newer is required (vps-setup.md §4.3)" in result.stderr
        assert said in result.stderr

    def test_refused_although_cosign_2_verifies_the_release(self, ready: World) -> None:
        """The fake verifies every signature: only the version check stops it, as on the pilot
        host before M11 (where a real cosign 2 then refused every release as unsigned)."""
        ready.cosign_version(fixture("cosign-version-v2.5.0.txt"))
        refused_before_anything(ready)
        assert ready.deployed()[-1][1] == "1.0.0"


class TestResume:
    """``--resume`` after a failure behind the gates: checked the same way, gates left on."""

    @pytest.fixture
    def failed(self, ready: World) -> World:
        ready.fail_on("web verify-internal")
        assert ready.run("deploy.sh", VERSION).returncode != 0
        ready.recover()
        ready.clear_logs()
        assert ready.gates() == (True, True)
        return ready

    def test_cosign_2_is_refused_and_the_gates_stay_on(self, failed: World) -> None:
        failed.cosign_version(fixture("cosign-version-v2.5.0.txt"))
        result = refused_before_anything(failed, "--resume")
        assert "cosign 3 or newer is required" in result.stderr
        assert failed.state("STATUS") == "failed"

    def test_a_builtin_driver_is_refused_and_the_gates_stay_on(self, failed: World) -> None:
        failed.rootless()
        failed.rootlesskit_info(fixture("rootlesskit-info-builtin.json"))
        result = refused_before_anything(failed, "--resume")
        assert "hides the visitors' addresses" in result.stderr

    def test_a_host_set_up_right_resumes_as_before(self, failed: World) -> None:
        failed.rootless()
        failed.rootlesskit_info(fixture("rootlesskit-info-slirp4netns.json"))
        deployed(failed, "--resume")
        assert failed.gates() == (False, False)


# --- the rootless daemon's port driver ------------------------------------------------------------


class TestPortDriver:
    def test_the_api_answer_is_used_and_named(self, rootless: World) -> None:
        rootless.rootlesskit_info(fixture("rootlesskit-info-slirp4netns.json"))
        rootless.rootlesskit_cmdline(arguments("rootlesskit-cmdline-builtin.txt"))
        result = deployed(rootless)
        assert "port driver: slirp4netns (tcp, tcp4, udp, udp4)" in result.stderr

    def test_builtin_from_the_api_is_refused_saying_why_and_the_fix(self, rootless: World) -> None:
        """The real answer of a daemon set up without the runbook's override."""
        rootless.rootlesskit_info(fixture("rootlesskit-info-builtin.json"))
        result = refused_before_anything(rootless)
        for said in (
            "the port driver builtin (tcp, tcp4, tcp6, udp, udp4, udp6) hides the visitors'",
            "allauth's per-address rate limits would count every visitor as one",
            "one attacker's failed sign-ins would lock everyone out",
            "the audit log's addresses would mean nothing",
            OVERRIDE,
            "~/.config/systemd/user/docker.service.d/override.conf",
            "systemctl --user restart docker",
        ):
            assert said in result.stderr, said

    def test_the_api_wins_over_the_command_line(self, rootless: World) -> None:
        rootless.rootlesskit_info(fixture("rootlesskit-info-builtin.json"))
        refused_before_anything(rootless)  # the command line says slirp4netns

    def test_a_driver_without_udp_is_refused(self, rootless: World) -> None:
        answer = json.loads(fixture("rootlesskit-info-slirp4netns.json"))
        answer["portDriver"]["protos"] = ["tcp", "tcp4"]
        rootless.rootlesskit_info(json.dumps(answer))
        result = refused_before_anything(rootless)
        assert "does not carry UDP: the production overlay publishes 443/udp" in result.stderr

    @pytest.mark.parametrize(
        ("name", "allowed"),
        [("rootlesskit-cmdline-builtin.txt", False), ("rootlesskit-cmdline-slirp4netns.txt", True)],
        ids=["builtin", "slirp4netns named last"],
    )
    def test_without_the_api_the_command_line_decides(
        self, rootless: World, name: str, allowed: bool
    ) -> None:
        rootless.rootlesskit_cmdline(arguments(name))
        if allowed:
            assert "port driver: slirp4netns" in deployed(rootless).stderr
        else:
            assert OVERRIDE in refused_before_anything(rootless).stderr

    @pytest.mark.parametrize(
        "answer", ["not json", "{}", '{"portDriver": {}}'], ids=["not json", "empty", "no driver"]
    )
    def test_an_api_answer_without_a_driver_leaves_it_to_the_command_line(
        self, rootless: World, answer: str
    ) -> None:
        rootless.rootlesskit_info(answer)
        rootless.rootlesskit_cmdline(arguments("rootlesskit-cmdline-builtin.txt"))
        assert "hides the visitors' addresses" in refused_before_anything(rootless).stderr

    @pytest.mark.parametrize(
        "cmdline",
        [None, ["/proc/self/exe", "--net=slirp4netns", "/usr/bin/dockerd-rootless.sh"]],
        ids=["unreadable", "no option"],
    )
    def test_no_signal_warns_and_points_at_the_runbooks_check(
        self, rootless: World, cmdline: list[str] | None
    ) -> None:
        rootless.rootlesskit_cmdline(cmdline)
        result = deployed(rootless)
        assert "WARNING: the rootless daemon's port driver cannot be read" in result.stderr
        assert "/account/sessions/" in result.stderr
        assert "vps-setup.md §4" in result.stderr

    def test_a_root_daemon_has_none_to_check(self, ready: World) -> None:
        """Its answer would be refused, but a root daemon publishes ports itself."""
        ready.rootlesskit_info(fixture("rootlesskit-info-builtin.json"))
        assert "port driver" not in deployed(ready).stderr

    def test_the_daemon_is_checked_before_its_driver_is_read(self, rootless: World) -> None:
        """A pid file that names no dockerd: refused by the target checks, never read on."""
        (rootless.runtime / "docker.pid").write_text("1\n", encoding="utf-8")
        rootless.rootlesskit_info(fixture("rootlesskit-info-slirp4netns.json"))
        result = refused_before_anything(rootless)
        assert "does not name" in result.stderr


# --- the docker group -----------------------------------------------------------------------------


class TestDockerGroup:
    WARNING = f"WARNING: {ME} is in the docker group, which is root on this host"

    @pytest.mark.parametrize(
        "entry",
        [
            f"docker:x:999999:{ME}",
            f"docker:x:999999:someone,{ME},other",
            f"docker:x:{os.getgid()}:",
        ],
        ids=["member", "one of several", "this session's group"],
    )
    @pytest.mark.parametrize("daemon", ["root", "rootless"])
    def test_a_member_is_warned_and_goes_ahead(self, ready: World, entry: str, daemon: str) -> None:
        if daemon == "rootless":
            ready.rootless()
        ready.docker_group(entry)
        result = deployed(ready)
        assert self.WARNING in result.stderr
        assert "vps-setup.md §4, §8" in result.stderr

    @pytest.mark.parametrize(
        "entry",
        ["docker:x:999999:", f"docker:x:999999:{ME}x,x{ME}", None],
        ids=["empty", "names that only contain mine", "no group"],
    )
    def test_nobody_in_it_says_nothing(self, ready: World, entry: str | None) -> None:
        ready.docker_group(entry)
        assert "docker group" not in deployed(ready).stderr


# --- recover.sh status ----------------------------------------------------------------------------


class TestStatus:
    """Read-only, and it answers whatever the daemon does."""

    def status(self, world: World) -> str:
        found = world.gates()
        result = world.run("recover.sh", "status")
        assert result.returncode == 0, result.stderr
        assert "Decision tree:" in result.stdout
        assert world.gates() == found
        assert not world.nsenter_log()
        return result.stdout

    def test_a_root_daemon(self, ready: World) -> None:
        out = self.status(ready)
        assert "docker daemon:     root\n" in out
        assert "port driver:       none, a root daemon publishes ports itself" in out

    def test_a_rootless_daemon_that_keeps_the_address(self, rootless: World) -> None:
        rootless.rootlesskit_info(fixture("rootlesskit-info-slirp4netns.json"))
        out = self.status(rootless)
        assert "docker daemon:     rootless\n" in out
        assert (
            "port driver:       slirp4netns (tcp, tcp4, udp, udp4), keeps the visitor's address"
            in out
        )

    def test_a_rootless_daemon_that_hides_it(self, rootless: World) -> None:
        rootless.rootlesskit_info(fixture("rootlesskit-info-builtin.json"))
        out = self.status(rootless)
        assert "port driver:       refused by deploy.sh: the port driver builtin" in out

    def test_a_driver_no_source_names(self, rootless: World) -> None:
        rootless.rootlesskit_cmdline(None)
        out = self.status(rootless)
        assert "port driver:       unknown: neither RootlessKit's API nor its command line" in out

    def test_a_daemon_that_cannot_be_checked(self, rootless: World) -> None:
        (rootless.runtime / "docker.pid").unlink()
        out = self.status(rootless)
        assert "docker daemon:     rootless\n" in out
        assert "port driver:       unknown: " in out
        assert "docker.pid is missing" in out

    @pytest.mark.parametrize(
        ("daemon", "said"),
        [("down", "the Docker daemon does not answer"), ("userns", "remaps users")],
    )
    def test_a_daemon_the_scripts_do_not_run_under(
        self, ready: World, daemon: str, said: str
    ) -> None:
        ready.daemon(daemon)
        out = self.status(ready)
        assert "docker daemon:     unknown: " in out
        assert said in out
        assert "port driver" not in out


# --- the rehearsal's stand-in ---------------------------------------------------------------------


def test_the_rehearsals_cosign_answers_as_the_runbooks(tmp_path: Path) -> None:
    """The rehearsal runs deploy.sh with a stand-in cosign: its version passes the preflight."""
    source = (ROOT / "tests/rehearsal/conftest.py").read_text(encoding="utf-8")
    stand_in = next(
        node.value.value
        for node in ast.parse(source).body
        if isinstance(node, ast.Assign)
        and [getattr(target, "id", "") for target in node.targets] == ["FAKE_COSIGN"]
        and isinstance(node.value, ast.Constant)
    )
    script = tmp_path / "cosign"
    script.write_text(str(stand_in), encoding="utf-8")
    version = subprocess.run(  # noqa: S603 (the repository's stand-in)
        [sys.executable, str(script), "version"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout
    checks = ROOT / "docker_compose/gmat_m8/ops/checks.py"
    result = subprocess.run(  # noqa: S603 (the repository's script)
        [sys.executable, str(checks), "cosign-version", "-"],
        input=version, capture_output=True, text=True, check=False, timeout=30,
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
