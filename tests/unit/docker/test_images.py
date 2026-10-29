"""Static checks of the image sources (plan §7.1, §8.3, §9).

The images themselves are built and exercised by CI (`trivy-image`) and by the compose
smoke test; these tests keep the sources honest on every PR without Docker: pins, line
endings, the entry point's command table and the Gunicorn settings.
"""

import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
from django.conf import settings

ROOT = Path(__file__).resolve().parents[3]
DOCKER = ROOT / "docker"
DOCKERFILES = sorted(DOCKER.glob("*/Dockerfile"))
DIGEST_PIN = re.compile(r"@sha256:[0-9a-f]{64}\b")
ENTRYPOINT_COMMANDS = {
    "web",
    "runserver",
    "worker",
    "migrate",
    "check",
    "migrate-plan",
    "verify-internal",
    "post-restore-reconcile",
    "healthcheck",
    "manage",
}


def _usable_shell(name: str) -> str | None:
    """A working bash or sh, else None (on Windows ``bash`` can be a WSL stub without a distro)."""
    path = shutil.which(name)
    if path is None:
        return None
    probe = subprocess.run([path, "-c", "exit 0"], capture_output=True, check=False)  # noqa: S603
    return path if probe.returncode == 0 else None


BASH = _usable_shell("bash")
SH = _usable_shell("sh")


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _load_module(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True  # keep docker/ free of __pycache__
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


def test_both_images_have_a_dockerfile() -> None:
    assert [path.parent.name for path in DOCKERFILES] == ["app", "backup"]


@pytest.mark.parametrize("path", DOCKERFILES, ids=lambda p: p.parent.name)
def test_external_images_are_pinned_by_digest(path: Path) -> None:
    text = _text(path)
    image_args = re.findall(r"^ARG \w*IMAGE=(\S+)$", text, re.MULTILINE)
    assert image_args, f"{path.parent.name}: no pinned image argument"
    for reference in image_args:
        assert DIGEST_PIN.search(reference), f"{reference} is not pinned by digest"
    # Every FROM names an ARG, an earlier stage or a digest-pinned image.
    stages: set[str] = set()
    for match in re.finditer(r"^FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+(\S+))?", text, re.M):
        reference, stage = match.groups()
        if not (reference.startswith("${") or reference in stages):
            assert DIGEST_PIN.search(reference), f"{reference} is not pinned by digest"
        if stage:
            stages.add(stage)
    assert ":latest" not in text


@pytest.mark.parametrize("path", DOCKERFILES, ids=lambda p: p.parent.name)
def test_images_do_not_run_as_root(path: Path) -> None:
    assert "USER 1000:1000" in _text(path)


def test_downloaded_binaries_are_verified() -> None:
    app = _text(DOCKER / "app" / "Dockerfile")
    backup = _text(DOCKER / "backup" / "Dockerfile")
    assert "sha256sum --check --strict" in app  # Tailwind CLI
    assert "sha256sum -c" in backup  # supercronic
    for text in (app, backup):
        assert not re.search(r"curl[^\n]*\|\s*(ba)?sh", text), "never pipe a download to a shell"


def test_app_image_ships_no_pip_and_collects_static_without_secrets() -> None:
    text = _text(DOCKER / "app" / "Dockerfile")
    assert "pip uninstall" in text
    assert "DJANGO_SETTINGS_MODULE=config.settings.build" in text
    assert "uv sync --frozen --no-dev" in text


def test_backup_image_drops_the_unused_privilege_helper() -> None:
    assert "rm -f /usr/local/bin/gosu" in _text(DOCKER / "backup" / "Dockerfile")


def test_image_sources_use_lf_line_endings() -> None:
    sources = (p for p in DOCKER.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    for path in sources:
        assert b"\r" not in path.read_bytes(), f"{path.relative_to(ROOT)} has CRLF line endings"


def test_entrypoint_dispatches_every_documented_command() -> None:
    text = _text(DOCKER / "app" / "entrypoint.sh")
    labels = set(re.findall(r"^    ([a-z][a-z-]*)\)$", text, re.MULTILINE))
    assert labels == ENTRYPOINT_COMMANDS
    assert "set -euo pipefail" in text
    assert "check --deploy --fail-level WARNING" in text
    assert "migrate --plan" in text  # read only


@pytest.mark.skipif(BASH is None or SH is None, reason="needs bash and sh")
def test_shell_scripts_parse() -> None:
    scripts = [*DOCKER.rglob("*.sh")]
    assert scripts
    for script in scripts:
        shell = SH if script.parent.name == "initdb" else BASH
        result = subprocess.run(  # noqa: S603 (fixed arguments, repository files)
            [str(shell), "-n", str(script)], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, f"{script.name}: {result.stderr}"


class TestGunicornConf:
    PATH = DOCKER / "app" / "gunicorn.conf.py"

    def _load(self, monkeypatch: pytest.MonkeyPatch, forwarded: str | None) -> ModuleType:
        if forwarded is None:
            monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)
        else:
            monkeypatch.setenv("FORWARDED_ALLOW_IPS", forwarded)
        return _load_module(self.PATH, "gunicorn_conf_under_test")

    def test_access_log_is_off_and_caddy_is_the_only_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conf = self._load(monkeypatch, "172.30.0.0/24")
        assert conf.accesslog is None
        assert conf.worker_class == "gthread"
        assert conf.threads == 4
        assert conf.workers == 3
        assert conf.timeout == 60
        assert conf.control_socket_disable is True

    def test_forwarded_headers_come_from_the_edge_subnet_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert self._load(monkeypatch, "172.30.0.0/24").forwarded_allow_ips == "172.30.0.0/24"

    @pytest.mark.parametrize("value", [None, "", "  ", "*", "172.30.0.0/24,*"])
    def test_refuses_to_start_without_a_proper_proxy_allowlist(
        self, monkeypatch: pytest.MonkeyPatch, value: str | None
    ) -> None:
        with pytest.raises(SystemExit, match="FORWARDED_ALLOW_IPS"):
            self._load(monkeypatch, value)

    def test_errors_go_through_the_redaction_filter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        conf = self._load(monkeypatch, "172.30.0.0/24")
        handler = conf.logconfig_dict["handlers"]["console"]
        assert handler["filters"] == ["redact"]
        assert conf.logconfig_dict["filters"]["redact"]["()"] == "config.logging.RedactionFilter"
        assert conf.logconfig_dict["formatters"]["json"]["()"] == "config.logging.JsonFormatter"


class TestBackupScripts:
    def test_dumps_exclude_exactly_the_session_and_cache_data(self) -> None:
        text = _text(DOCKER / "backup" / "common.sh")
        match = re.search(r"EXCLUDED_DATA_TABLES=\(([^)]*)\)", text)
        assert match
        excluded = set(match.group(1).split())
        cache_table = settings.CACHES["default"]["LOCATION"]
        assert excluded == {"django_session", cache_table}

    def test_backup_follows_the_snapshot_protocol(self) -> None:
        backup = _text(DOCKER / "backup" / "backup.sh")
        common = _text(DOCKER / "backup" / "common.sh")
        order = [
            "pg_advisory_lock",
            "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY",
            "pg_export_snapshot",
            "pg_dump --format=custom --snapshot=",
            "COMMIT;",
        ]
        positions = [backup.index(fragment) for fragment in order]
        assert positions == sorted(positions)
        assert "gmat:media-backup" in common
        assert "--exclude-table-data=" in backup

    def test_offsite_upload_never_prunes(self) -> None:
        text = _text(DOCKER / "backup" / "backup.sh")
        assert "restic backup" in text
        assert not re.search(r"restic\s+(forget|prune|rm)\b", text)

    def test_schedule_is_every_six_hours_plus_a_weekly_verify(self) -> None:
        lines = [
            line
            for line in _text(DOCKER / "backup" / "crontab").splitlines()
            if line.strip() and not line.startswith("#")
        ]
        assert lines[0].startswith("0 */6 * * * ")
        assert "restore.sh --verify" in lines[1]


@pytest.mark.skipif(SH is None, reason="needs sh")
class TestRoleScript:
    SCRIPT = DOCKER / "postgres" / "initdb" / "10-roles.sh"

    STRONG = {
        "POSTGRES_DB": "gmat",
        "POSTGRES_USER": "gmat_admin",
        "POSTGRES_PASSWORD": "Zq3-bootstrap-superuser-x9Kp27LmQ",
        "GMAT_DB_OWNER_USER": "gmat_owner",
        "GMAT_DB_OWNER_PASSWORD": "Vb8rT2nKd4Wq7Ys1Hj6Lm3PzXc5Fg0Ua",
        "GMAT_DB_RUNTIME_USER": "gmat_app",
        "GMAT_DB_RUNTIME_PASSWORD": "Qe5uN9aRt1Oi8Pk4Zs7Mv2Bx6Cy3Dw0H",
        "GMAT_DB_BACKUP_USER": "gmat_backup",
        "GMAT_DB_BACKUP_PASSWORD": "Lk2jH7gF4dS9aP1oI8uY5tR3eW6qZ0xC",
    }

    def _run(self, **overrides: str) -> subprocess.CompletedProcess[str]:
        env = {"PATH": os.environ.get("PATH", ""), **self.STRONG, **overrides}
        # Every case below fails validation before psql would be reached.
        return subprocess.run(  # noqa: S603
            [str(SH), str(self.SCRIPT)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    @pytest.mark.parametrize(
        ("override", "message"),
        [
            ({"GMAT_DB_OWNER_PASSWORD": "short"}, "at least 24"),
            ({"GMAT_DB_RUNTIME_PASSWORD": "changethis-changethis-changethis"}, "placeholder"),
            ({"GMAT_DB_BACKUP_PASSWORD": "my-password-is-long-enough-yes"}, "placeholder"),
            ({"GMAT_DB_OWNER_USER": "Bad-Name"}, "must match"),
            ({"GMAT_DB_RUNTIME_USER": "postgres"}, "reserved"),
            ({"GMAT_DB_RUNTIME_USER": "gmat_owner"}, "must differ"),
            ({"GMAT_DB_BACKUP_USER": ""}, "is required"),
            (
                {"GMAT_DB_RUNTIME_PASSWORD": "Vb8rT2nKd4Wq7Ys1Hj6Lm3PzXc5Fg0Ua"},
                "passwords must differ",
            ),
            (
                {"POSTGRES_PASSWORD": "Lk2jH7gF4dS9aP1oI8uY5tR3eW6qZ0xC"},
                "equals the bootstrap",
            ),
        ],
    )
    def test_weak_or_inconsistent_input_is_refused(
        self, override: dict[str, str], message: str
    ) -> None:
        result = self._run(**override)
        assert result.returncode == 1
        assert message in result.stderr
