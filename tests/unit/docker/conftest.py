"""A compose directory with fake ``docker``, ``cosign``, ``curl`` and ``nsenter``: the host
scripts' world.

``deploy.sh``, ``recover.sh``, ``restore-drill.sh`` and ``init.sh`` run for real in a copy of the
stack directory. The fakes log what they were asked, with the state of both gates at that moment,
and fail on request. The same scripts against real containers are ``tests/rehearsal``.

The fake ``docker`` answers as a root daemon unless ``World.rootless()`` makes it a rootless one
(ADR-0033). Then ``backups/`` is closed (mode 0000), as the containers' shifted uid's directory is
to the host account, and only the fake ``nsenter`` opens it, for the one command it runs: a
script that reads or writes there without the helper fails. A real rootless daemon cannot run
here, so its process tree is a fake ``/proc`` and its subordinate ids fake ``/etc/subuid`` and
``/etc/subgid`` files: the copy of ``ops/common.sh`` is pointed at them, and at this account's own
uid and gid as the containers' (an unprivileged test cannot give a file to uid 1000). Those
constants are the only lines of a script a test rewrites; ``test_backups_helper`` checks their
values in the repository. M12.5 rehearses the scripts against a real rootless daemon.
"""

import json
import os
import pwd
import shutil
import stat
import subprocess
import textwrap
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]
STACK = ROOT / "docker_compose" / "gmat_m8"
APP_DIGEST = "sha256:" + "a" * 64
BACKUP_DIGEST = "sha256:" + "b" * 64
OLD_APP = "sha256:" + "c" * 64
OLD_BACKUP = "sha256:" + "d" * 64
PREFIX = "registry.test/mano8"
REPOSITORY = "mano8/gmat-m8"
# The fake rootless daemon's processes: dockerd, and the rootlesskit that started it.
DOCKERD_PID = 4242
ROOTLESSKIT_PID = 4241
DAEMON_OPTIONS = {
    "root": '["name=apparmor","name=seccomp,profile=builtin","name=cgroupns"]',
    "rootless": '["name=seccomp,profile=builtin","name=rootless","name=cgroupns"]',
    "userns": '["name=apparmor","name=seccomp,profile=builtin","name=userns","name=cgroupns"]',
}

FAKE_DOCKER = r"""#!/usr/bin/env python3
import os, pathlib, sys
fake = pathlib.Path(os.environ["FAKE_DIR"])
args = sys.argv[1:]
while len(args) >= 2 and args[0] == "compose" and args[1] == "-f":
    args = ["compose", *args[3:]]
line = " ".join(" ".join(args).split())
gates = ("M" if pathlib.Path("flags/maintenance.on").exists() else "-") + (
    "W" if pathlib.Path("flags/worker.paused").exists() else "-"
)
with (fake / "docker.log").open("a") as log:
    log.write(f"{gates} {line}\n")
failing = fake / "fail"
for pattern in failing.read_text().splitlines() if failing.exists() else []:
    if pattern and pattern in line:
        print(f"fake docker refuses: {line}", file=sys.stderr)
        sys.exit(3)
sql = sys.stdin.read() if ("exec -T db" in line and not sys.stdin.isatty()) else ""
if "pg_restore" in line:
    (fake / "pg_restore.in").write_text(sql)
if line.split(" ")[0] == "info":
    daemon = fake / "daemon"
    kind = daemon.read_text().strip() if daemon.exists() else "root"
    if kind == "down":
        print("Cannot connect to the Docker daemon", file=sys.stderr)
        sys.exit(1)
    print(os.environ["FAKE_OPTIONS_" + kind.upper()])
elif line.startswith("context inspect"):
    context = fake / "context"
    print(context.read_text().strip() if context.exists() else "unix:///var/run/docker.sock")
elif "--entrypoint python web" in line:
    (fake / "python.in").write_text(sys.stdin.read())
elif "'row_counts'" in sql:
    print('{"row_counts": {"core_thing": 3}}')
elif "config --images" in line:
    print(f"{os.environ['FAKE_PREFIX']}/gmat-m8@{os.environ['APP_DIGEST']}")
    print(f"{os.environ['FAKE_PREFIX']}/gmat-m8-backup@{os.environ['BACKUP_DIGEST']}")
    print("caddy:fake@sha256:aaaa")
    print("postgres:fake@sha256:bbbb")
elif line.startswith("compose ps") or line.startswith("volume create"):
    print("container")
elif line.startswith("image inspect"):
    print(args[-1])
elif line.startswith("inspect"):
    print("postgres:fake")
elif "backup run --wait" in line:
    label = args[args.index("--label") + 1]
    print(f"gmat-20261105T000000Z-{label}")
elif "WORKER_EXPECT_STATE=paused" in line:
    sys.exit(0 if pathlib.Path("flags/worker.paused").exists() else 1)
elif "WORKER_EXPECT_STATE=" in line:
    sys.exit(1 if pathlib.Path("flags/worker.paused").exists() else 0)
elif "pg_dump" in line:
    print("FAILED-DUMP")
elif "to_regclass('public.django_migrations') IS NOT NULL;" in sql and "DISTINCT" not in sql:
    print("t")
elif "DISTINCT ON" in sql:
    print((fake / "head").read_text() if (fake / "head").exists() else "core 0001_x")
elif "format_version" in sql:
    print('{"format_version": 1, "audit": [], "outbox": []}')
elif "post-restore-reconcile" in line:
    print('{"failed_state_used": true}')
"""

FAKE_COSIGN = r"""#!/usr/bin/env python3
import os, pathlib, sys
fake = pathlib.Path(os.environ["FAKE_DIR"])
line = " ".join(sys.argv[1:])
with (fake / "cosign.log").open("a") as log:
    log.write(line + "\n")
failing = fake / "cosign_fail"
for pattern in failing.read_text().splitlines() if failing.exists() else []:
    if pattern and pattern in line:
        sys.exit(1)
"""

FAKE_CURL = r"""#!/usr/bin/env python3
import os, pathlib, shutil, sys
fake = pathlib.Path(os.environ["FAKE_DIR"])
args = sys.argv[1:]
url = next(a for a in reversed(args) if a.startswith("http"))
if "--config" in args:
    (fake / "curl_config").write_text(sys.stdin.read())
if "--write-out" in args:
    public = fake / "public_status"
    open_status = public.read_text().strip() if public.exists() else "200"
    print("503" if pathlib.Path("flags/maintenance.on").exists() else open_status, end="")
    sys.exit(0)
name = url.rsplit("/", 1)[-1]
source = fake / "release" / ("release.json" if "/releases/tags/" in url else name)
if not source.exists():
    sys.exit(22)
shutil.copy(source, args[args.index("--output") + 1])
"""

# Enters nothing: it checks that it was asked for the daemon's user namespace alone and a tool by
# its absolute path, then runs the command with backups/ open, as it is to the namespace's root.
FAKE_NSENTER = r"""#!/usr/bin/env python3
import os, pathlib, subprocess, sys
fake = pathlib.Path(os.environ["FAKE_DIR"])
args = sys.argv[1:]
with (fake / "nsenter.log").open("a") as log:
    log.write(" ".join(args) + "\n")
pid = (pathlib.Path(os.environ["XDG_RUNTIME_DIR"]) / "docker.pid").read_text().strip()
if args[:5] != ["-U", "--preserve-credentials", "-t", pid, "--"]:
    sys.exit(f"fake nsenter: not the daemon's user namespace alone: {args}")
command = args[5:]
if not command or not command[0].startswith("/usr/bin/"):
    sys.exit(f"fake nsenter: not a tool named by its path: {command}")
backups = pathlib.Path("backups")
if backups.is_dir():
    backups.chmod(0o700)
try:
    code = subprocess.run(command, check=False).returncode
finally:
    if backups.is_dir():
        backups.chmod(0o000)
sys.exit(code)
"""


def _executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@dataclass
class World:
    """A stack directory, its fakes and a way to run the scripts in it."""

    stack: Path
    fake: Path
    env: dict[str, str]

    def run(self, script: str, *args: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 (a copy of the repository's scripts)
            [str(self.stack / script), *args],
            cwd=self.stack,
            env=self.env,
            input=stdin,
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )

    def fail_on(self, *patterns: str) -> None:
        (self.fake / "fail").write_text("\n".join(patterns), encoding="utf-8")

    def recover(self) -> None:
        """The next run may succeed."""
        (self.fake / "fail").unlink(missing_ok=True)

    def docker_log(self) -> list[str]:
        path = self.fake / "docker.log"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def cosign_log(self) -> list[str]:
        path = self.fake / "cosign.log"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def clear_logs(self) -> None:
        for name in ("docker.log", "cosign.log", "nsenter.log"):
            (self.fake / name).unlink(missing_ok=True)

    def gates(self) -> tuple[bool, bool]:
        flags = self.stack / "flags"
        return (flags / "maintenance.on").exists(), (flags / "worker.paused").exists()

    def env_value(self, key: str, file: str = ".env") -> str:
        for line in (self.stack / file).read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1]
        return ""

    def deployed(self) -> list[list[str]]:
        path = self.stack / "deployed.log"
        if not path.exists():
            return []
        return [line.split("\t") for line in path.read_text(encoding="utf-8").splitlines()]

    def state(self, key: str) -> str:
        return self.env_value(key, "deploy.state")

    def release(
        self,
        version: str,
        /,
        *,
        notes: str = "Notes",
        irreversible: str = "",
        old_code_works: str = "",
        **changes: Any,
    ) -> None:
        """What the fake GitHub serves for the next run: signed manifest and unsigned notes."""
        release = self.fake / "release"
        release.mkdir(exist_ok=True)
        manifest = {
            "schema_version": 1,
            "version": version,
            "tag": f"v{version}",
            "commit": "e" * 40,
            "repository": REPOSITORY,
            "images": {
                "app": {
                    "repository": f"{PREFIX}/gmat-m8",
                    "digest": APP_DIGEST,
                    "ref": f"{PREFIX}/gmat-m8@{APP_DIGEST}",
                },
                "backup": {
                    "repository": f"{PREFIX}/gmat-m8-backup",
                    "digest": BACKUP_DIGEST,
                    "ref": f"{PREFIX}/gmat-m8-backup@{BACKUP_DIGEST}",
                },
            },
            "migration": {"irreversible": irreversible, "old_code_works": old_code_works},
            **changes,
        }
        (release / "release-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (release / "release-manifest.json.sigstore.json").write_text("{}", encoding="utf-8")
        (release / "release.json").write_text(
            json.dumps(
                {
                    "body": notes,
                    "assets": [
                        {"name": name, "url": f"https://github.test/assets/{name}"}
                        for name in ("release-manifest.json", "release-manifest.json.sigstore.json")
                    ],
                }
            ),
            encoding="utf-8",
        )

    def previously_deployed(self) -> None:
        (self.stack / "deployed.log").write_text(
            f"2026-11-01T00:00:00Z\t1.0.0\t{OLD_APP}\t{OLD_BACKUP}\tgmat-old\tdeploy\n",
            encoding="utf-8",
        )
        for key, value in (("APP_VERSION", "1.0.0"), ("APP_DIGEST", OLD_APP)):
            self._set_env(key, value)
        self._set_env("BACKUP_DIGEST", OLD_BACKUP)

    def state_of_a_finished_deploy(self) -> None:
        (self.stack / "deploy.state").write_text("STEP=11\nSTATUS=done\n", encoding="utf-8")

    def _set_env(self, key: str, value: str) -> None:
        path = self.stack / ".env"
        lines = [
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if not line.startswith(f"{key}=")
        ]
        path.write_text("\n".join([*lines, f"{key}={value}"]) + "\n", encoding="utf-8")

    def _drop_env(self, key: str) -> None:
        path = self.stack / ".env"
        lines = path.read_text(encoding="utf-8").splitlines()
        kept = [line for line in lines if not line.startswith(f"{key}=")]
        path.write_text("\n".join(kept) + "\n", encoding="utf-8")

    # --- the daemon (ADR-0033) ------------------------------------------------------------

    def daemon(self, kind: str) -> None:
        """What ``docker info`` answers: root, rootless, userns, or down."""
        (self.fake / "daemon").write_text(kind, encoding="utf-8")

    @property
    def proc(self) -> Path:
        return self.fake / "proc"

    @property
    def runtime(self) -> Path:
        return self.fake / "run"

    def rootless(self, *, fake_proc: bool = True) -> None:
        """A rootless daemon: its pid file, its socket for the CLI, its process tree, and
        ``backups/`` closed to this account."""
        if os.getuid() == 0 or os.getgid() == 0:
            pytest.skip("root reads every file: the closed backups/ would prove nothing")
        self.daemon("rootless")
        self.runtime.mkdir(exist_ok=True)
        (self.runtime / "docker.pid").write_text(f"{DOCKERD_PID}\n", encoding="utf-8")
        self.env["XDG_RUNTIME_DIR"] = str(self.runtime)
        self.env["DOCKER_HOST"] = f"unix://{self.runtime}/docker.sock"
        if fake_proc:
            self._fake_process_tree()
            user = pwd.getpwuid(os.getuid()).pw_name
            for name in ("subuid", "subgid"):
                (self.fake / name).write_text(f"{user}:100000:65536\n", encoding="utf-8")
            self.point_at(
                PROC_ROOT=self.proc,
                SUBUID_FILE=self.fake / "subuid",
                SUBGID_FILE=self.fake / "subgid",
            )
        backups = self.stack / "backups"
        if backups.is_dir():
            backups.chmod(0o000)

    def point_at(self, **constants: Any) -> None:
        """Rewrites constants of the copy of ``ops/common.sh`` (their repository values are
        checked by ``test_backups_helper``)."""
        common = self.stack / "ops" / "common.sh"
        text = common.read_text(encoding="utf-8")
        for name, value in constants.items():
            lines = [line for line in text.splitlines() if line.startswith(f"{name}=")]
            assert len(lines) == 1, f"{name} is not one constant of ops/common.sh"
            text = text.replace(lines[0], f"{name}={value}")
        common.write_text(text, encoding="utf-8")

    def _fake_process_tree(self) -> None:
        dockerd = self.proc / str(DOCKERD_PID)
        (dockerd / "ns").mkdir(parents=True, exist_ok=True)
        (dockerd / "exe").symlink_to("/usr/bin/dockerd")
        (dockerd / "status").write_text(f"Name:\tdockerd\nPPid:\t{ROOTLESSKIT_PID}\n")
        (dockerd / "ns" / "user").symlink_to("user:[4026532200]")
        (dockerd / "uid_map").write_text(f"         0 {os.getuid():>10}          1\n"
                                         "         1     100000      65536\n")  # fmt: skip
        (dockerd / "gid_map").write_text(f"         0 {os.getgid():>10}          1\n"
                                         "         1     100000      65536\n")  # fmt: skip
        kit = self.proc / str(ROOTLESSKIT_PID)
        kit.mkdir()
        (kit / "exe").symlink_to("/usr/bin/rootlesskit")
        (kit / "status").write_text("Name:\trootlesskit\nPPid:\t1\n")
        (self.proc / "self" / "ns").mkdir(parents=True)
        (self.proc / "self" / "ns" / "user").symlink_to("user:[4026531837]")

    @contextmanager
    def opened_backups(self) -> Iterator[Path]:
        """``backups/`` open to the test for a moment, then as the scripts left it."""
        backups = self.stack / "backups"
        mode = stat.S_IMODE(backups.stat().st_mode)
        backups.chmod(0o700)
        try:
            yield backups
        finally:
            backups.chmod(mode)

    def put_backup(self, name: str, text: str = "DUMP") -> None:
        with self.opened_backups() as backups:
            target = backups / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")

    def backup_names(self) -> set[str]:
        with self.opened_backups() as backups:
            return {path.name for path in backups.iterdir()}

    def nsenter_log(self) -> list[str]:
        path = self.fake / "nsenter.log"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def restored(self) -> str:
        """What the last ``pg_restore`` read on its stdin."""
        return (self.fake / "pg_restore.in").read_text(encoding="utf-8")


ENV_FILE = textwrap.dedent(
    """\
    APP_DOMAIN=gmat.test
    ADMIN_ALLOWED_CIDRS=203.0.113.0/24
    RESTIC_REPOSITORY=rest:https://backup.test/gmat
    RESTIC_PASSWORD=restic-offline-copy
    GITHUB_READ_TOKEN=fake-read-token
    APP_DIGEST=
    BACKUP_DIGEST=
    APP_VERSION=dev
    """
)


def _checkout(root: Path) -> Path:
    """The parts of a checkout the host scripts reach: the stack directory and what it reads
    from ``docker/`` (``init.sh``'s role scripts, the drill's manifest query)."""
    stack = root / "docker_compose" / "gmat_m8"
    stack.mkdir(parents=True)
    for name in ("deploy.sh", "recover.sh", "restore-drill.sh", "init.sh"):
        shutil.copy2(STACK / name, stack / name)
    for name in (".env.example", "app.env.example"):
        shutil.copy2(STACK / name, stack / name)
    shutil.copytree(STACK / "ops", stack / "ops", ignore=shutil.ignore_patterns("__pycache__"))
    (stack / "clamav").mkdir()
    for name in ("clamd.conf", "freshclam.conf"):
        shutil.copy2(STACK / "clamav" / name, stack / "clamav" / name)
    shutil.copytree(ROOT / "docker/postgres/initdb", root / "docker/postgres/initdb")
    (root / "docker/backup").mkdir(parents=True)
    shutil.copy2(ROOT / "docker/backup/manifest.sql", root / "docker/backup/manifest.sql")
    return stack


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    stack = _checkout(tmp_path / "checkout")
    (stack / "backups").mkdir(mode=0o700)
    (stack / ".env").write_text(ENV_FILE, encoding="utf-8")
    (stack / ".env").chmod(0o600)
    (stack / "app.env").write_text("DJANGO_SECRET_KEY=old-key-value\n", encoding="utf-8")
    (stack / "app.env").chmod(0o600)
    fake = tmp_path / "fake"
    fake.mkdir()
    bin_dir = fake / "bin"
    bin_dir.mkdir()
    _executable(bin_dir / "docker", FAKE_DOCKER)
    _executable(bin_dir / "cosign", FAKE_COSIGN)
    _executable(bin_dir / "curl", FAKE_CURL)
    _executable(bin_dir / "nsenter", FAKE_NSENTER)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_DIR": str(fake),
        "FAKE_PREFIX": PREFIX,
        **{f"FAKE_OPTIONS_{kind.upper()}": value for kind, value in DAEMON_OPTIONS.items()},
        "DEPLOY_IMAGE_PREFIX": PREFIX,
        "DEPLOY_GITHUB_REPOSITORY": REPOSITORY,
        "DEPLOY_LOCK_FILE": str(fake / "lock"),
        "GITHUB_API_URL": "https://github.test",
    }
    for name in ("COMPOSE_FILE", "DOCKER_HOST", "DOCKER_CONTEXT", "XDG_RUNTIME_DIR"):
        env.pop(name, None)
    world = World(stack=stack, fake=fake, env=env)
    world.point_at(CONTAINER_UID=os.getuid(), CONTAINER_GID=os.getgid())
    yield world
    backups = stack / "backups"
    if backups.is_dir():
        backups.chmod(0o700)
