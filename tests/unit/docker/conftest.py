"""A compose directory with fake ``docker``, ``cosign`` and ``curl``: the host scripts' world.

``deploy.sh`` and ``recover.sh`` run for real in a copy of the stack directory. The fakes log
what they were asked, with the state of both gates at that moment, and fail on request. The
same scripts against real containers are ``tests/rehearsal``.
"""

import json
import os
import shutil
import stat
import subprocess
import textwrap
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
if "config --images" in line:
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
        (self.fake / "docker.log").unlink(missing_ok=True)
        (self.fake / "cosign.log").unlink(missing_ok=True)

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


@pytest.fixture
def world(tmp_path: Path) -> World:
    stack = tmp_path / "stack"
    stack.mkdir()
    for name in ("deploy.sh", "recover.sh", "restore-drill.sh"):
        shutil.copy2(STACK / name, stack / name)
    shutil.copytree(STACK / "ops", stack / "ops", ignore=shutil.ignore_patterns("__pycache__"))
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
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_DIR": str(fake),
        "FAKE_PREFIX": PREFIX,
        "DEPLOY_IMAGE_PREFIX": PREFIX,
        "DEPLOY_GITHUB_REPOSITORY": REPOSITORY,
        "DEPLOY_LOCK_FILE": str(fake / "lock"),
        "GITHUB_API_URL": "https://github.test",
    }
    env.pop("COMPOSE_FILE", None)
    return World(stack=stack, fake=fake, env=env)
