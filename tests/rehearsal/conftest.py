"""The world of the deploy rehearsal (plan M8.4, "Done when"): real containers, nothing faked
but GitHub and the keyless signatures, which only a tag push in Actions can produce.

- a throwaway copy of the compose directory, with its own project name, ports and volumes;
- a local registry, with vN, vN+1 and the broken releases built from this checkout;
- a small HTTP server standing for GitHub's API (``GITHUB_API_URL``);
- ``cosign`` on PATH: a stand-in that knows what was "signed", or the real one in a container
  where the rehearsal proves a refusal (an unsigned image must fail real verification);
- a restic REST server pair: append-only for the VPS, delete-capable for the maintainer.

    sg docker -c "REHEARSAL_REQUIRED=1 uv run pytest tests/rehearsal -m rehearsal -s"

Without ``REHEARSAL_REQUIRED=1`` everything skips. It takes about 25 minutes, more on a machine
short of memory, and leaves only Docker's build cache behind unless ``REHEARSAL_KEEP=1``.
"""

import hashlib
import http.server
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import pytest

from tests.live.conftest import Response, Stack

ROOT = Path(__file__).resolve().parents[2]
SOURCE_STACK = ROOT / "docker_compose" / "gmat_m8"
OVERLAY = Path(__file__).with_name("docker-compose.rehearsal.yml")
REGISTRY_PORT = 5055
PREFIX = f"localhost:{REGISTRY_PORT}/mano8"
REPOSITORY = "mano8/gmat-m8"
PROJECT = "gmat-rehearsal"
HTTPS_PORT = 18443
ADMIN_PORT = 18001
COSIGN_IMAGE = (
    "ghcr.io/sigstore/cosign/cosign@"
    "sha256:e82eb6d42ccb6bc048d8d9e5e598e4d5178e1af6c00e54e02c9b0569c5f3ec11"
)
REGISTRY_IMAGE = "registry@sha256:a3d8aaa63ed8681a604f1dea0aa03f100d5895b6a58ace528858a7b332415373"
COMPOSE_FILES = (
    "docker-compose.yml",
    "docker-compose.production.yml",
    "docker-compose.local-mail.yml",
    "docker-compose.rehearsal.yml",
)
ADMIN_EMAIL = "admin@demo.example"
PASSWORD = "Rehearsal-pass-2026!"
OFFSITE_PASSWORD = "rehearsal-offsite-offline-copy"
LATEST_MIGRATION_DIR = ROOT / "apps" / "notifications" / "migrations"

FAKE_COSIGN = r"""#!/usr/bin/env python3
# Stands for keyless signatures, or runs the real cosign on the verbs REHEARSAL_REAL_COSIGN names.
import hashlib, json, os, subprocess, sys

args = sys.argv[1:]
verb = args[0]
if verb in os.environ.get("REHEARSAL_REAL_COSIGN", "").split(","):
    cwd = os.getcwd()
    sys.exit(subprocess.call(
        ["docker", "run", "--rm", "--network", "host", "-v", f"{cwd}:{cwd}:ro", "-w", cwd,
         os.environ["REHEARSAL_COSIGN_IMAGE"], *args]))
signed = json.load(open(os.environ["REHEARSAL_SIGNED"]))
identity = args[args.index("--certificate-identity") + 1]
issuer = args[args.index("--certificate-oidc-issuer") + 1]
if issuer != "https://token.actions.githubusercontent.com":
    sys.exit(1)
if verb == "verify-blob":
    with open(args[-1], "rb") as handle:
        subject = hashlib.sha256(handle.read()).hexdigest()
else:
    subject = args[-1].rsplit("@", 1)[-1]
sys.exit(0 if f"{identity} {subject}" in signed else 1)
"""


def run(*command: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 (fixed tools, test input only)
        command, capture_output=True, text=True, check=True, **kwargs
    )


def docker(*args: str, **kwargs: Any) -> str:
    return str(run("docker", *args, **kwargs).stdout).strip()


def identity_of(version: str) -> str:
    return f"https://github.com/{REPOSITORY}/.github/workflows/docker-publish.yaml@refs/tags/v{version}"


@dataclass
class Gh:
    """Stands for GitHub's API: the release JSON and its assets."""

    releases: dict[str, dict[str, Any]] = field(default_factory=dict)
    assets: dict[str, bytes] = field(default_factory=dict)
    url: str = ""

    def start(self) -> None:
        state = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                body = state.answer(self.path)
                self.send_response(200 if body is not None else 404)
                self.end_headers()
                self.wfile.write(body or b"")

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def answer(self, path: str) -> bytes | None:
        found = re.fullmatch(rf"/repos/{REPOSITORY}/releases/tags/v(.+)", path)
        if found and found[1] in self.releases:
            return json.dumps(self.releases[found[1]]).encode()
        return self.assets.get(path)

    def publish(self, version: str, manifest: bytes, notes: str) -> None:
        names = ("release-manifest.json", "release-manifest.json.sigstore.json")
        self.assets[f"/assets/{version}/{names[0]}"] = manifest
        self.assets[f"/assets/{version}/{names[1]}"] = b"{}"
        self.releases[version] = {
            "tag_name": f"v{version}",
            "body": notes,
            "assets": [
                {"name": name, "url": f"{self.url}/assets/{version}/{name}"} for name in names
            ],
        }


@dataclass
class World:
    """One installation: a compose directory, its project, its scripts' environment."""

    directory: Path
    stack: Stack
    env: dict[str, str]
    gh: Gh
    signed_file: Path
    images: dict[str, str]

    @property
    def compose_dir(self) -> Path:
        return self.stack.compose_dir

    # --- the scripts ---------------------------------------------------------------------

    def script(
        self, name: str, *args: str, check: bool = True, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(  # noqa: S603 (a copy of the repository's scripts)
            [str(self.compose_dir / name), *args],
            cwd=self.compose_dir,
            env={**self.env, **(env or {})},
            capture_output=True,
            text=True,
            timeout=1800,
            check=False,
        )
        if check and result.returncode != 0:
            raise AssertionError(f"{name} {args} failed:\n{result.stdout}\n{result.stderr}")
        return result

    def start(self, name: str, *args: str) -> subprocess.Popen[str]:
        return subprocess.Popen(  # noqa: S603 (a copy of the repository's scripts)
            [str(self.compose_dir / name), *args],
            cwd=self.compose_dir,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def deploy(self, version: str, *args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return self.script("deploy.sh", version, *args, **kwargs)

    def recover(self, *args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return self.script("recover.sh", *args, **kwargs)

    # --- releases ------------------------------------------------------------------------

    def sign(self, identity: str, subject: str) -> None:
        signed = json.loads(self.signed_file.read_text(encoding="utf-8"))
        signed.append(f"{identity} {subject}")
        self.signed_file.write_text(json.dumps(signed), encoding="utf-8")

    def release(
        self,
        version: str,
        image: str,
        *,
        notes: str = "",
        signed: bool = True,
        backup_image: str = "backup",
    ) -> None:
        """Publish ``image`` as ``version``: its manifest on the fake GitHub, signed or not."""
        app = self.images[image]
        backup = self.images[backup_image]
        manifest = json.dumps(
            {
                "schema_version": 1,
                "version": version,
                "tag": f"v{version}",
                "commit": "0" * 40,
                "repository": REPOSITORY,
                "images": {
                    "app": {
                        "repository": f"{PREFIX}/gmat-m8",
                        "digest": app,
                        "ref": f"{PREFIX}/gmat-m8@{app}",
                    },
                    "backup": {
                        "repository": f"{PREFIX}/gmat-m8-backup",
                        "digest": backup,
                        "ref": f"{PREFIX}/gmat-m8-backup@{backup}",
                    },
                },
            }
        ).encode()
        self.gh.publish(version, manifest, notes)
        if signed:
            identity = identity_of(version)
            self.sign(identity, hashlib.sha256(manifest).hexdigest())
            self.sign(identity, app)
            self.sign(identity, backup)

    # --- inside the stack ----------------------------------------------------------------

    def compose(self, *args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return self.stack.compose(*args, **kwargs)

    def manage(self, *args: str, dev: bool = False) -> str:
        command = ["run", "--rm", "--no-deps", "-T"]
        if dev:
            command += ["-e", "DJANGO_SETTINGS_MODULE=config.settings.dev"]
        return str(self.compose(*command, "web", "manage", *args, timeout=600).stdout)

    def python(self, code: str) -> Any:
        """Run ``code`` with Django set up in a one-off ``web``; it prints one JSON line."""
        out = self.compose(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "--entrypoint",
            "python",
            "web",
            "-c",
            f"import django, json\ndjango.setup()\n{code}",
        ).stdout
        return json.loads(out.strip().splitlines()[-1])

    def sql(self, statement: str) -> str:
        return self.stack.sql(statement)

    def env_value(self, key: str, file: str = ".env") -> str:
        for line in (self.compose_dir / file).read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1]
        return ""

    def deployed(self) -> list[list[str]]:
        path = self.compose_dir / "deployed.log"
        if not path.exists():
            return []
        return [row.split("\t") for row in path.read_text(encoding="utf-8").splitlines()]

    def gates(self) -> tuple[bool, bool]:
        flags = self.compose_dir / "flags"
        return (flags / "maintenance.on").exists(), (flags / "worker.paused").exists()

    def running_image(self, service: str) -> str:
        container = self.compose("ps", "-q", service).stdout.strip()
        return docker("inspect", "--format", "{{.Config.Image}}", container)

    def worker_state(self) -> str:
        return self.compose(
            "exec",
            "-T",
            "worker",
            "python",
            "-c",
            "import json; print(json.load(open('/tmp/gmat-worker.heartbeat'))['state'])",
        ).stdout.strip()


class Browser:
    """What a browser keeps between requests: cookies, and CSRF from the page it is on."""

    CSRF = re.compile(r'name="csrfmiddlewaretoken"\s+value="([^"]+)"')
    ACTION = re.compile(
        r'<form[^>]*\baction="([^"]+)"[^>]*\bmethod="post"|<form[^>]*\bmethod="post"[^>]*\baction="([^"]+)"',
        re.I,
    )

    def __init__(self, stack: Stack) -> None:
        self.stack = stack
        self.cookies: dict[str, str] = {}

    def _keep(self, response: Response) -> Response:
        for value in response.headers_named("set-cookie"):
            name, _, rest = value.partition("=")
            self.cookies[name] = rest.split(";", 1)[0]
        return response

    def _cookie_header(self) -> dict[str, str]:
        cookies = "; ".join(f"{k}={v}" for k, v in self.cookies.items() if v)
        return {"Accept-Language": "en", **({"Cookie": cookies} if cookies else {})}

    def get(self, path: str) -> Response:
        return self._keep(self.stack.get(path, headers=self._cookie_header()))

    def follow(self, path: str, limit: int = 6) -> tuple[str, Response]:
        response = self.get(path)
        for _ in range(limit):
            if response.status not in {301, 302, 303}:
                break
            path = response.header("location") or "/"
            response = self.get(path)
        return path, response

    def post(self, path: str, page: Response, **fields: str) -> Response:
        token = self.CSRF.search(page.text)
        assert token, f"no CSRF token on the page before {path}"
        body = "&".join(
            f"{name}={quote_plus(value)}"
            for name, value in {"csrfmiddlewaretoken": token[1], **fields}.items()
        )
        return self._keep(
            self.stack.request(
                "POST",
                path,
                body=body.encode(),
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": self.stack.base_url,
                    "Referer": f"{self.stack.base_url}{path}",
                    **self._cookie_header(),
                },
            )
        )

    def sign_in(self, email: str, password: str = PASSWORD) -> Response:
        page = self.get("/accounts/login/")
        return self.post("/accounts/login/", page, login=email, password=password)

    def second_factor(self, code: str) -> Response:
        page = self.get("/accounts/2fa/authenticate/")
        return self.post("/accounts/2fa/authenticate/", page, code=code)

    @property
    def session(self) -> str:
        return self.cookies.get("__Host-sessionid", "")


def _prepare_directory(target: Path) -> None:
    """A checkout's shape: the compose directory, and what it mounts from ``docker/``."""
    (target / "docker" / "postgres").mkdir(parents=True)
    shutil.copytree(ROOT / "docker" / "postgres" / "initdb", target / "docker/postgres/initdb")
    (target / "docker" / "backup").mkdir()
    shutil.copy2(ROOT / "docker/backup/manifest.sql", target / "docker/backup/manifest.sql")
    stack = target / "docker_compose" / "gmat_m8"
    shutil.copytree(
        SOURCE_STACK,
        stack,
        ignore=shutil.ignore_patterns(
            "__pycache__",
            ".env",
            "app.env",
            "backups",
            "flags",
            "deployed.log",
            "deploy.state",
            "deploy.head",
            ".release",
            "preserved",
            "drill",
        ),
    )
    shutil.copy2(OVERLAY, stack / OVERLAY.name)


def _edit_template(stack: Path, values: dict[str, str]) -> None:
    template = stack / ".env.example"
    lines = template.read_text(encoding="utf-8").splitlines()
    kept = [line for line in lines if line.split("=", 1)[0] not in values]
    template.write_text(
        "\n".join([*kept, *(f"{key}={value}" for key, value in values.items())]) + "\n",
        encoding="utf-8",
    )


def make_world(
    root: Path, project: str, https_port: int, subnet: str, base: dict[str, Any]
) -> World:
    """A fresh installation under ``root``; ``base`` holds what installations share."""
    root.mkdir(parents=True)
    _prepare_directory(root)
    stack_dir = root / "docker_compose" / "gmat_m8"
    http_port = https_port - 363
    _edit_template(
        stack_dir,
        {
            "APP_DOMAIN": "localhost",
            "ACME_EMAIL": "ops@demo.example",
            "HTTP_PORT": str(http_port),
            "HTTPS_PORT": str(https_port),
            "EDGE_SUBNET": subnet,
            "ADMIN_ALLOWED_CIDRS": "203.0.113.0/24",
            "RESTIC_REPOSITORY": "rest:http://rest-append:8000/gmat",
            "RESTIC_PASSWORD": OFFSITE_PASSWORD,
            "APP_DIGEST": "sha256:" + "0" * 64,
            "BACKUP_DIGEST": "sha256:" + "0" * 64,
        },
    )
    run("bash", "init.sh", cwd=stack_dir)
    env = {
        **os.environ,
        "PATH": f"{base['bin']}{os.pathsep}{os.environ['PATH']}",
        "COMPOSE_PROJECT_NAME": project,
        "DEPLOY_COMPOSE_FILES": ":".join(COMPOSE_FILES),
        "DEPLOY_LOCK_FILE": str(root / "lock"),
        "DEPLOY_IMAGE_PREFIX": PREFIX,
        "DEPLOY_PUBLIC_PORT": str(https_port),
        "GITHUB_API_URL": base["gh"].url,
        "REHEARSAL_SIGNED": str(base["signed"]),
        "REHEARSAL_COSIGN_IMAGE": base["cosign_image"],
    }
    os.environ.update(
        {
            "REHEARSAL_PREFIX": PREFIX,
            "REHEARSAL_OFFSITE": str(base["offsite"]),
            "REHEARSAL_ADMIN_PORT": str(ADMIN_PORT),
        }
    )
    env.update(
        {
            "REHEARSAL_PREFIX": PREFIX,
            "REHEARSAL_OFFSITE": str(base["offsite"]),
            "REHEARSAL_ADMIN_PORT": str(ADMIN_PORT),
            "REHEARSAL_HTTP_PORT": str(http_port),
            "REHEARSAL_HTTPS_PORT": str(https_port),
        }
    )
    (stack_dir / ".rehearsal-ports").write_text(f"{http_port} {https_port}\n", encoding="utf-8")
    _append_env(stack_dir / ".env", http_port, https_port)
    return World(
        directory=root,
        stack=Stack(
            base_url=f"https://localhost:{https_port}",
            compose_dir=stack_dir,
            compose_files=list(COMPOSE_FILES),
            project=project,
        ),
        env=env,
        gh=base["gh"],
        signed_file=base["signed"],
        images=base["images"],
    )


def _append_env(path: Path, http_port: int, https_port: int) -> None:
    """The overlay's own ports, also for ``docker compose`` calls that read only ``.env``."""
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"REHEARSAL_HTTP_PORT={http_port}\nREHEARSAL_HTTPS_PORT={https_port}\n")


# --- images ------------------------------------------------------------------------------------


def _build(tag: str, dockerfile: str) -> None:
    run("docker", "build", "-q", "-t", tag, "-", input=dockerfile)


def _publish(tag: str, repository: str, version: str) -> str:
    target = f"{PREFIX}/{repository}:{version}"
    docker("tag", tag, target)
    docker("push", "-q", target)
    digests = json.loads(docker("inspect", "--format", "{{json .RepoDigests}}", target))
    found = next(item for item in digests if item.startswith(f"{PREFIX}/{repository}@"))
    return str(found.rsplit("@", 1)[1])


def _migration(name: str, dependency: str, up: str, down: str) -> str:
    """A ``RUN`` that adds a migration of the notifications app to an image."""
    return f"""RUN cat > /app/apps/notifications/migrations/{name}.py <<'PY'
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("notifications", "{dependency}")]
    operations = [migrations.RunSQL({up!r}, {down!r})]
PY
"""


def _variant(base_tag: str, *runs: str) -> str:
    body = "".join(runs)
    return f"# syntax=docker/dockerfile:1\nFROM {base_tag}\nUSER 0:0\n{body}USER 1000:1000\n"


def build_images() -> dict[str, str]:
    """vN, vN+1 and the broken releases, built from this checkout, pushed by digest."""
    app, backup = "gmat-rehearsal-app:base", "gmat-rehearsal-backup:base"
    dockerfile = str(ROOT / "docker/app/Dockerfile")
    run("docker", "build", "-q", "-f", dockerfile, "--target", "runtime", "-t", app, str(ROOT))
    run(
        "docker",
        "build",
        "-q",
        "-f",
        str(ROOT / "docker/backup/Dockerfile"),
        "-t",
        backup,
        str(ROOT),
    )
    latest = max(path.stem for path in LATEST_MIGRATION_DIR.glob("0*.py"))
    table_a = _migration(
        "0090_rehearsal_a",
        latest,
        "CREATE TABLE rehearsal_a (id integer PRIMARY KEY)",
        "DROP TABLE rehearsal_a",
    )
    table_b = _migration(
        "0091_rehearsal_b",
        "0090_rehearsal_a",
        "CREATE TABLE rehearsal_b (id integer PRIMARY KEY)",
        "DROP TABLE rehearsal_b",
    )
    breaks = _migration("0092_rehearsal_fails", "0091_rehearsal_b", "SELECT 1/0", "SELECT 1")
    # /readyz/ fails but /healthz/ answers: only the internal verification can notice.
    bad_ready = (
        'RUN sed -i \'s/cursor.execute("SELECT 1")/raise RuntimeError("forced")/\' '
        "/app/apps/core/views.py\n"
    )
    variants = {
        "next": _variant(app, table_a),
        "bad-smoke": _variant(f"{PREFIX}/gmat-m8:next", table_b, bad_ready),
        "bad-migration": _variant(f"{PREFIX}/gmat-m8:next", table_b, breaks),
        "fix-a": _variant(f"{PREFIX}/gmat-m8:next", 'LABEL rehearsal="fix-a"\n'),
        "fix-b": _variant(f"{PREFIX}/gmat-m8:next", 'LABEL rehearsal="fix-b"\n'),
    }
    images = {
        "base": _publish(app, "gmat-m8", "base"),
        "backup": _publish(backup, "gmat-m8-backup", "base"),
    }
    for name, dockerfile_text in variants.items():
        tag = f"gmat-rehearsal-app:{name}"
        _build(tag, dockerfile_text)
        images[name] = _publish(tag, "gmat-m8", name)
    # Its dumps cannot start: a deploy stops at step 6, before the migration.
    _build("gmat-rehearsal-backup:bad", _variant(backup, "RUN rm /usr/local/bin/pg_dump\n"))
    images["bad-backup"] = _publish("gmat-rehearsal-backup:bad", "gmat-m8-backup", "bad")
    return images


def _executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _remove_project(project: str) -> None:
    env = {**os.environ, "COMPOSE_PROJECT_NAME": project}
    containers = run(
        "docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"
    ).stdout.split()
    if containers:
        subprocess.run(["docker", "rm", "-f", *containers], capture_output=True, check=False)  # noqa: S603, S607
    for name in run("docker", "volume", "ls", "-q", "--filter", f"name={project}_").stdout.split():
        subprocess.run(["docker", "volume", "rm", "-f", name], capture_output=True, check=False)  # noqa: S603, S607
    for name in run("docker", "network", "ls", "-q", "--filter", f"name={project}_").stdout.split():
        subprocess.run(["docker", "network", "rm", name], capture_output=True, check=False, env=env)  # noqa: S603, S607


PROJECTS = (PROJECT, f"{PROJECT}-new", f"{PROJECT}-drill-a", f"{PROJECT}-drill-b")


def _clean() -> None:
    subprocess.run(
        ["docker", "rm", "-f", "gmat-rehearsal-registry"],  # noqa: S607
        capture_output=True,
        check=False,
    )
    for project in PROJECTS:
        _remove_project(project)
    for reference in ("gmat-rehearsal-*", f"localhost:{REGISTRY_PORT}/mano8/*"):
        images = docker("images", "-q", "--filter", f"reference={reference}").split()
        if images:
            subprocess.run(  # noqa: S603
                ["docker", "image", "rm", "-f", *sorted(set(images))],  # noqa: S607
                capture_output=True,
                check=False,
            )


@pytest.fixture(scope="session")
def base() -> Iterator[dict[str, Any]]:
    if os.environ.get("REHEARSAL_REQUIRED") != "1":
        pytest.skip("the deploy rehearsal needs REHEARSAL_REQUIRED=1 and Docker (about 25 minutes)")
    _clean()
    root = Path(tempfile.mkdtemp(prefix="gmat-rehearsal-", dir=os.environ.get("REHEARSAL_DIR")))
    docker(
        "run",
        "-d",
        "--name",
        "gmat-rehearsal-registry",
        "-p",
        f"127.0.0.1:{REGISTRY_PORT}:5000",
        REGISTRY_IMAGE,
    )
    offsite = root / "offsite"
    offsite.mkdir()
    fake_bin = root / "bin"
    fake_bin.mkdir()
    _executable(fake_bin / "cosign", FAKE_COSIGN)
    signed = root / "signed.json"
    signed.write_text("[]", encoding="utf-8")
    gh = Gh()
    gh.start()
    try:
        yield {
            "root": root,
            "bin": fake_bin,
            "signed": signed,
            "offsite": offsite,
            "gh": gh,
            "images": build_images(),
            "cosign_image": COSIGN_IMAGE,
        }
    finally:
        gh.server.shutdown()
        if os.environ.get("REHEARSAL_KEEP") != "1":
            _clean()
            shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(scope="session")
def world(base: dict[str, Any]) -> World:
    installation = make_world(base["root"] / "vps", PROJECT, HTTPS_PORT, "172.30.81.0/24", base)
    installation.compose(
        "up", "-d", "--no-deps", "--no-build", "mailpit", "rest-append", "rest-admin"
    )
    return installation
