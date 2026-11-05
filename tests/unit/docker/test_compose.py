"""Static checks of the compose stack's worker and mail setup (plan §7.2, §5).

The running stack is exercised by ``tests/live``; these keep the files honest without
Docker: the worker's networks and hardening, where the SMTP password goes, the local-only
Mailpit, and the overlays.
"""

import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
STACK = ROOT / "docker_compose" / "gmat_m8"
ENTRYPOINT = ROOT / "docker" / "app" / "entrypoint.sh"


class _ComposeLoader(yaml.SafeLoader):
    """Reads compose's merge tags (``!reset``, ``!override``) as plain values."""


_ComposeLoader.add_multi_constructor(
    "!",
    lambda loader, suffix, node: (
        loader.construct_sequence(node)
        if isinstance(node, yaml.SequenceNode)
        else loader.construct_mapping(node)
        if isinstance(node, yaml.MappingNode)
        else loader.construct_scalar(node)
    ),
)


def compose(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.load((STACK / name).read_text(encoding="utf-8"), _ComposeLoader)  # noqa: S506 (SafeLoader subclass)
    return loaded


@pytest.fixture(scope="module")
def base() -> dict[str, Any]:
    return compose("docker-compose.yml")


def test_the_worker_shares_no_network_with_web(base: dict[str, Any]) -> None:
    services = base["services"]
    assert services["worker"]["networks"] == ["db_worker", "egress"]
    assert not set(services["worker"]["networks"]) & set(services["web"]["networks"])
    assert base["networks"]["db_worker"]["internal"] is True


def test_the_worker_is_hardened_like_web(base: dict[str, Any]) -> None:
    worker = base["services"]["worker"]
    for key in ("read_only", "cap_drop", "security_opt", "user", "tmpfs", "restart"):
        assert worker[key] == base["services"]["web"][key], key
    assert "ports" not in worker and "expose" not in worker
    assert worker["volumes"] == ["./flags:/srv/flags:ro", "media:/srv/media"]
    assert worker["healthcheck"]["test"][-1] == "worker-healthcheck"
    # An in-flight send may use its whole 60 s deadline before the container stops.
    assert worker["stop_grace_period"] == "75s"


def test_the_media_volume_is_private_to_web_worker_and_backup(base: dict[str, Any]) -> None:
    """Attachments (M6.4): web stores, the worker purges, the backup only reads; Caddy, which
    serves files, never sees the volume, so nothing bypasses the authorised download."""
    services = base["services"]
    mounts = {
        name: [volume for volume in service.get("volumes", []) if volume.startswith("media:")]
        for name, service in services.items()
    }
    assert mounts == {
        "caddy": [],
        "migrate": [],
        "web": ["media:/srv/media"],
        "worker": ["media:/srv/media"],
        "db": [],
        "backup": ["media:/srv/media:ro"],
        "decoder": [],
        "clamd": [],
        "freshclam": [],
    }
    assert services["backup"]["environment"]["MEDIA_DIR"] == "/srv/media/files"
    assert "media" in base["volumes"]


def test_the_smtp_password_reaches_the_worker_only(base: dict[str, Any]) -> None:
    for name, service in base["services"].items():
        environment = service.get("environment", {})
        assert ("EMAIL_HOST_PASSWORD" in environment) == (name == "worker"), name
    for template in ("app.env.example", "app.env.production.example"):
        assert "EMAIL_HOST_PASSWORD" not in (STACK / template).read_text(encoding="utf-8")


def test_mailpit_is_a_local_sink_only(base: dict[str, Any]) -> None:
    assert "mailpit" not in base["services"]  # not in the production image set
    for name in ("docker-compose.local-mail.yml", "docker-compose.dev.yml"):
        mailpit = compose(name)["services"]["mailpit"]
        assert mailpit["networks"] == ["egress"], name
        assert mailpit["read_only"] is True and mailpit["user"] == "1000:1000", name
        assert mailpit["cap_drop"] == ["ALL"], name
        assert re.search(r"@sha256:[0-9a-f]{64}$", mailpit["image"]), name
    assert "ports" not in compose("docker-compose.local-mail.yml")["services"]["mailpit"]
    assert compose("docker-compose.dev.yml")["services"]["mailpit"]["ports"] == [
        "127.0.0.1:8025:8025"
    ]
    env = (STACK / ".env.example").read_text(encoding="utf-8")
    assert "COMPOSE_FILE=docker-compose.yml:docker-compose.local-mail.yml" in env
    assert "COMPOSE_PATH_SEPARATOR=:" in env
    assert "mailpit" not in compose("docker-compose.production.yml")["services"]


def test_production_runs_the_worker_from_the_signed_digest() -> None:
    worker = compose("docker-compose.production.yml")["services"]["worker"]
    assert worker["image"] == "ghcr.io/mano8/gmat-m8@${APP_DIGEST:?set APP_DIGEST in .env}"


SOCKET_SERVICES = ("decoder", "clamd")


@pytest.mark.parametrize("name", SOCKET_SERVICES)
def test_what_parses_uploads_has_no_way_to_anything(base: dict[str, Any], name: str) -> None:
    """M8.3, X7 and X11: no network, no media, no database, no application secret."""
    service = base["services"][name]
    assert service["network_mode"] == "none"
    assert "networks" not in service
    assert "env_file" not in service
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert not any(volume.startswith("media:") for volume in service["volumes"])
    assert not set(service.get("environment", {})) - {"ATTACHMENT_DECODER_SOCKET"}


def test_the_decoder_runs_under_its_own_uid(base: dict[str, Any]) -> None:
    services = base["services"]
    assert services["decoder"]["user"] == "1001:1001"
    assert services["web"]["user"] == "1000:1000"
    assert "uid=1001" in services["decoder"]["tmpfs"][0]
    assert "uid=1001" in base["volumes"]["decoder_socket"]["driver_opts"]["o"]


@pytest.mark.parametrize(
    ("volume", "service", "path"),
    [
        ("clamd_socket", "clamd", "/run/gmat/clamd"),
        ("decoder_socket", "decoder", "/run/gmat/decoder"),
    ],
)
def test_each_socket_is_shared_only_with_web_which_cannot_replace_it(
    base: dict[str, Any], volume: str, service: str, path: str
) -> None:
    users = {
        name: mount
        for name, definition in base["services"].items()
        for mount in definition.get("volumes", [])
        if str(mount).startswith(f"{volume}:")
    }
    assert users == {service: f"{volume}:{path}", "web": f"{volume}:{path}:ro"}


def test_the_sockets_are_where_production_settings_look() -> None:
    settings = (ROOT / "config" / "settings" / "prod.py").read_text(encoding="utf-8")
    assert '"/run/gmat/clamd/clamd.sock"' in settings
    assert '"/run/gmat/decoder/decoder.sock"' in settings
    clamd = (STACK / "clamav" / "clamd.conf").read_text(encoding="utf-8")
    assert "LocalSocket /run/gmat/clamd/clamd.sock" in clamd
    assert "\nTCPSocket" not in clamd


def test_only_freshclam_reaches_out_and_it_sees_no_file(base: dict[str, Any]) -> None:
    freshclam = base["services"]["freshclam"]
    assert freshclam["networks"] == ["updates"]
    assert "internal" not in base["networks"]["updates"]
    assert set(freshclam["volumes"]) == {
        "./clamav/freshclam.conf:/etc/gmat/freshclam.conf:ro",
        "clamav_db:/var/lib/clamav",
    }
    assert "clamav_db:/var/lib/clamav:ro" in base["services"]["clamd"]["volumes"]
    users = [
        name
        for name, definition in base["services"].items()
        if "updates" in definition.get("networks", [])
    ]
    assert users == ["freshclam"]


def test_what_cannot_be_scanned_is_refused() -> None:
    clamd = (STACK / "clamav" / "clamd.conf").read_text(encoding="utf-8").splitlines()
    assert "AlertExceedsMax yes" in clamd
    assert "AlertEncryptedDoc yes" in clamd
    assert "MaxFileSize 16M" in clamd  # above an attachment's 10 MiB


def test_production_runs_the_decoder_from_the_signed_digest() -> None:
    decoder = compose("docker-compose.production.yml")["services"]["decoder"]
    assert decoder["image"].startswith("ghcr.io/mano8/gmat-m8@${APP_DIGEST")


def test_the_restore_drills_networks_are_all_internal(base: dict[str, Any]) -> None:
    drill = compose("docker-compose.restore-drill.yml")
    assert set(drill["networks"]) == set(base["networks"])
    assert all(network["internal"] is True for network in drill["networks"].values())


def test_production_needs_an_explicit_admin_allowlist() -> None:
    """M8.3, X10: the base file's open default never reaches production."""
    caddy = compose("docker-compose.production.yml")["services"]["caddy"]
    assert caddy["environment"]["ADMIN_ALLOWED_CIDRS"].startswith("${ADMIN_ALLOWED_CIDRS:?")


def test_the_restore_drill_never_sends() -> None:
    drill = compose("docker-compose.restore-drill.yml")
    assert drill["services"]["worker"]["environment"]["OUTBOUND_EMAIL"] == "off"
    assert all(network["internal"] for network in drill["networks"].values())


def test_templates_generate_an_outbox_key_and_point_at_mailpit() -> None:
    local = (STACK / "app.env.example").read_text(encoding="utf-8")
    assert "OUTBOX_ENCRYPTION_KEYS=__GENERATE_FERNET__" in local
    assert "EMAIL_HOST=mailpit" in local
    assert "OUTBOUND_EMAIL=on" in local


def _healthcheck_script() -> str:
    text = ENTRYPOINT.read_text(encoding="utf-8")
    match = re.search(r"worker-healthcheck\)\n.*?<<'PY'\n(.*?)\nPY\n", text, re.DOTALL)
    assert match, "the worker-healthcheck command embeds its script"
    return match.group(1)


@pytest.mark.parametrize(("age", "healthy"), [(5, True), (119, True), (121, False), (None, False)])
def test_the_worker_healthcheck_reads_the_heartbeat(
    tmp_path: Path, age: float | None, healthy: bool
) -> None:
    heartbeat = tmp_path / "heartbeat"
    if age is not None:
        heartbeat.write_text(json.dumps({"state": "paused", "at": time.time() - age}))
    result = subprocess.run(  # noqa: S603 (the interpreter running the tests)
        [sys.executable, "-c", _healthcheck_script()],
        env={"WORKER_HEARTBEAT_FILE": str(heartbeat)},
        check=False,
    )
    assert (result.returncode == 0) is healthy


@pytest.mark.parametrize(
    ("state", "expected", "healthy"),
    [
        ("paused", "paused", True),
        ("running", "paused", False),
        ("running", "running,outbound-off", True),
        ("outbound-off", "running,outbound-off", True),
        ("paused", "running,outbound-off", False),
        ("paused", "", True),
    ],
)
def test_a_deploy_can_ask_the_heartbeat_for_a_state(
    tmp_path: Path, state: str, expected: str, healthy: bool
) -> None:
    heartbeat = tmp_path / "heartbeat"
    heartbeat.write_text(json.dumps({"state": state, "at": time.time()}))
    result = subprocess.run(  # noqa: S603 (the interpreter running the tests)
        [sys.executable, "-c", _healthcheck_script()],
        env={"WORKER_HEARTBEAT_FILE": str(heartbeat), "WORKER_EXPECT_STATE": expected},
        check=False,
    )
    assert (result.returncode == 0) is healthy
