"""The edge in the host scripts (ADR-0034, M13.3), against fakes: what ``deploy.sh`` checks before
anything changes, Caddy validating its configuration before the gates, the tunnel's connector
started with Caddy behind them, and what ``init.sh`` prepares. The settings themselves are
``test_edge_checks``; the same scripts against real containers are ``tests/rehearsal``.
"""

import os
import stat
import subprocess
from pathlib import Path

import pytest

from tests.edge_certificates import write_edge_files
from tests.unit.docker.conftest import DOCKERD_PID, World
from tests.unit.docker.test_deploy_scripts import VERSION, failed_deploy, in_order, position
from tests.unit.docker.test_host_preflight import refused_before_anything

TUNNEL = "tunnel.gmat.test"
VALIDATE = "run --rm --no-deps -T -e XDG_DATA_HOME=/tmp/data -e XDG_CONFIG_HOME=/tmp/config"


def mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def files_mode(world: World) -> None:
    write_edge_files(world.stack, ["gmat.test"])
    world.set_env(EDGE_TLS="files")


def tunnel_mode(world: World) -> None:
    write_edge_files(world.stack, ["gmat.test", TUNNEL])
    token = world.stack / "tunnel" / "token"
    token.write_text("a-tunnel-token\n", encoding="utf-8")
    token.chmod(0o400)
    world.set_env(
        EDGE_TLS="files", EDGE_PROXY="cloudflare", EDGE_EXTRA_HOST=TUNNEL, COMPOSE_PROFILES="tunnel"
    )
    world.set_env("app.env", DJANGO_ALLOWED_HOSTS=f"gmat.test,{TUNNEL}")


@pytest.fixture
def ready(world: World) -> World:
    world.previously_deployed()
    world.release(VERSION)
    return world


class TestTheDefaultMode:
    def test_starts_no_connector(self, ready: World) -> None:
        result = ready.run("deploy.sh", VERSION)
        assert result.returncode == 0, result.stderr
        assert not [line for line in ready.docker_log() if "cloudflared" in line]
        assert "edge: EDGE_TLS=acme, EDGE_PROXY=none, the site answers gmat.test" in result.stderr
        assert "the links in email use https://gmat.test" in result.stderr

    def test_caddy_validates_its_configuration_before_the_gates(self, ready: World) -> None:
        """With its storage on its tmpfs: the running Caddy keeps its volumes as they are."""
        ready.run("deploy.sh", VERSION)
        log = ready.docker_log()
        validate = position(log, f"{VALIDATE} caddy caddy validate --config /etc/caddy/Caddyfile")
        assert log[validate].startswith("-- ")
        in_order(log, "pull --quiet caddy db clamd freshclam", "caddy validate", "web migrate-plan")

    def test_a_configuration_caddy_refuses_leaves_the_site_open(self, ready: World) -> None:
        ready.fail_on("caddy validate")
        result = ready.run("deploy.sh", VERSION)
        assert result.returncode != 0
        assert "Caddy refuses this installation's settings or files" in result.stderr
        assert ready.gates() == (False, False)
        assert "Stopped at step 3, before the gates" in result.stderr


class TestRefusedBeforeAnything:
    @pytest.mark.parametrize(
        ("settings", "reason"),
        [
            ({"EDGE_TLS": "ACME"}, "EDGE_TLS: 'ACME' is not one of acme, files"),
            ({"EDGE_PROXY": "cloudflare*"}, "EDGE_PROXY: 'cloudflare*' is not one of"),
            ({"EDGE_TUNNEL_ADDRESS": "0.0.0.0/0"}, "EDGE_TUNNEL_ADDRESS: '0.0.0.0/0'"),
            ({"EDGE_EXTRA_HOST": TUNNEL}, f"DJANGO_ALLOWED_HOSTS in app.env lacks {TUNNEL}"),
            ({"COMPOSE_PROFILES": "tunnel"}, "COMPOSE_PROFILES holds 'tunnel' if and only if"),
            ({"EDGE_TLS": "files"}, "tls/cert.pem is missing"),
        ],
    )  # fmt: skip
    def test_a_setting_deploy_would_trip_on(
        self, ready: World, settings: dict[str, str], reason: str
    ) -> None:
        ready.set_env(**settings)
        assert reason in refused_before_anything(ready).stderr

    def test_a_key_the_containers_cannot_read(self, ready: World) -> None:
        files_mode(ready)
        (ready.stack / "tls" / "key.pem").chmod(0o440)
        result = refused_before_anything(ready)
        uid, gid = os.getuid(), os.getgid()
        assert (
            f"tls/key.pem is uid {uid} mode 440 as the root daemon's containers see it, not uid "
            f"{uid} mode 400: sudo chown {uid}:{gid} tls/key.pem && sudo chmod 0400 tls/key.pem"
        ) in result.stderr

    def test_a_key_that_is_a_link(self, ready: World) -> None:
        files_mode(ready)
        key = ready.stack / "tls" / "key.pem"
        key.rename(ready.stack / "key.pem")
        key.symlink_to(ready.stack / "key.pem")
        result = refused_before_anything(ready)
        assert "tls/key.pem is missing or not a plain file" in result.stderr

    def test_a_token_the_containers_cannot_read(self, ready: World) -> None:
        tunnel_mode(ready)
        (ready.stack / "tunnel" / "token").chmod(0o644)
        assert "tunnel/token is uid" in refused_before_anything(ready).stderr

    def test_under_a_rootless_daemon_the_owner_is_read_in_its_namespace(self, ready: World) -> None:
        files_mode(ready)
        (ready.stack / "tls" / "key.pem").chmod(0o600)
        ready.rootless()
        result = refused_before_anything(ready)
        assert "as the rootless daemon's containers see it" in result.stderr
        assert "mode 400: run bash init.sh" in result.stderr
        prefix = f"-U --preserve-credentials -t {DOCKERD_PID} -- "
        stat_key = f"{prefix}/usr/bin/stat -c %u %a -- {ready.stack}/tls/key.pem"
        assert stat_key in ready.nsenter_log()


class TestFilesMode:
    def test_a_key_given_to_the_containers_deploys(self, ready: World) -> None:
        files_mode(ready)
        result = ready.run("deploy.sh", VERSION)
        assert result.returncode == 0, result.stderr
        assert "tls/cert.pem names gmat.test, valid until " in result.stderr
        assert not [line for line in ready.docker_log() if "cloudflared" in line]


class TestTheTunnelMode:
    def test_the_connector_follows_caddy_behind_the_gates(self, ready: World) -> None:
        tunnel_mode(ready)
        result = ready.run("deploy.sh", VERSION)
        assert result.returncode == 0, result.stderr
        log = ready.docker_log()
        in_order(
            log,
            "pull --quiet caddy db clamd freshclam",
            "pull --quiet cloudflared",
            "caddy validate",
            "run --rm --no-deps -T caddy-perms",
            "up -d --no-deps caddy",
            "up -d --no-deps cloudflared",
            "stop web worker",
        )
        assert not log[position(log, "pull --quiet cloudflared")].startswith("MW ")
        assert log[position(log, "up -d --no-deps cloudflared")].startswith("MW ")
        assert "Caddy believes CF-Connecting-IP from 172.30.90.250 alone" in result.stderr

    def test_a_connector_that_does_not_start_keeps_the_gates_on(self, ready: World) -> None:
        tunnel_mode(ready)
        result = failed_deploy(ready, "up -d --no-deps cloudflared")
        assert "the tunnel's connector (cloudflared) did not start" in result.stderr
        assert ready.gates() == (True, True)
        assert ready.state("STEP") == "4"

    def test_a_connector_that_cannot_be_pulled_leaves_the_site_open(self, ready: World) -> None:
        tunnel_mode(ready)
        ready.fail_on("pull --quiet cloudflared")
        result = ready.run("deploy.sh", VERSION)
        assert result.returncode != 0
        assert "the tunnel's connector could not be pulled" in result.stderr
        assert ready.gates() == (False, False)

    def test_recover_starts_it_with_a_caddy_that_was_down(self, ready: World) -> None:
        tunnel_mode(ready)
        failed_deploy(ready, "backup run --wait")
        ready.fail_on("ps --status running -q caddy")
        result = ready.run("recover.sh", "rollback")
        assert result.returncode == 0, result.stderr
        log = ready.docker_log()
        in_order(log, "up -d --no-deps caddy", "up -d --no-deps cloudflared", "stop web worker")
        assert log[position(log, "up -d --no-deps cloudflared")].startswith("MW ")


class TestInit:
    def run_init(self, world: World) -> subprocess.CompletedProcess[str]:
        result = world.run("init.sh")
        assert result.returncode == 0, result.stderr
        return result

    def test_prepares_the_directories_and_checks_the_settings(self, world: World) -> None:
        result = self.run_init(world)
        for name in ("tls", "tunnel"):
            assert mode(world.stack / name) == 0o755, name
        assert "init.sh: edge: EDGE_TLS=acme, EDGE_PROXY=none" in result.stdout
        assert "init.sh: the links in email use https://gmat.test" in result.stdout

    def test_refuses_a_setting_after_writing_the_env_files(self, world: World) -> None:
        (world.stack / ".env").unlink()
        (world.stack / "app.env").unlink()
        (world.stack / ".env.example").write_text(
            (world.stack / ".env.example").read_text(encoding="utf-8") + "EDGE_PROXY=Cloudflare\n",
            encoding="utf-8",
        )
        result = world.run("init.sh")
        assert result.returncode == 1
        assert "init.sh: EDGE_PROXY: 'Cloudflare' is not one of none, cloudflare" in result.stderr
        assert (world.stack / ".env").exists() and (world.stack / "app.env").exists()

    def test_opens_the_certificates_to_the_containers(self, world: World) -> None:
        write_edge_files(world.stack, ["gmat.test"])
        for name in ("tls/cert.pem", "tunnel/ca.pem"):
            (world.stack / name).chmod(0o600)
        self.run_init(world)
        for name in ("tls/cert.pem", "tunnel/ca.pem"):
            assert mode(world.stack / name) == 0o644, name

    def test_closes_a_key_that_is_the_containers_already(self, world: World) -> None:
        """Under a root daemon, when this account's uid is the containers'."""
        write_edge_files(world.stack, ["gmat.test"])
        (world.stack / "tls" / "key.pem").chmod(0o640)
        self.run_init(world)
        assert mode(world.stack / "tls" / "key.pem") == 0o400

    def test_says_how_to_give_a_key_away_under_a_root_daemon(self, world: World) -> None:
        write_edge_files(world.stack, ["gmat.test"])
        (world.stack / "tls" / "key.pem").chmod(0o600)
        world.point_at(CONTAINER_UID=4321, CONTAINER_GID=4321)
        result = self.run_init(world)
        assert (
            "the containers read it as uid 4321, so give it to them: sudo chown 4321:4321 "
            "tls/key.pem && sudo chmod 0400 tls/key.pem"
        ) in result.stderr
        assert mode(world.stack / "tls" / "key.pem") == 0o600

    def test_gives_the_secrets_to_the_containers_under_a_rootless_daemon(
        self, world: World
    ) -> None:
        tunnel_mode(world)
        for name in ("tls/key.pem", "tunnel/token"):
            (world.stack / name).chmod(0o600)
        world.rootless()
        self.run_init(world)
        prefix = f"-U --preserve-credentials -t {DOCKERD_PID} -- "
        owner = f"{os.getuid()}:{os.getgid()}"
        for name in ("tls/key.pem", "tunnel/token"):
            path = f"{world.stack}/{name}"
            assert f"{prefix}/usr/bin/chown --no-dereference {owner} -- {path}" in (
                world.nsenter_log()
            )
            assert f"{prefix}/usr/bin/chmod 0400 -- {path}" in world.nsenter_log()
            assert mode(world.stack / name) == 0o400, name

    @pytest.mark.parametrize("name", ["tls", "tunnel"])
    def test_a_directory_that_is_a_link_is_refused(self, world: World, name: str) -> None:
        (world.stack / "elsewhere").mkdir()
        (world.stack / name).symlink_to(world.stack / "elsewhere")
        result = world.run("init.sh")
        assert result.returncode != 0
        assert f"{name}/ is a symbolic link" in result.stderr

    def test_a_secret_that_is_a_link_is_never_given_away(self, world: World) -> None:
        write_edge_files(world.stack, ["gmat.test"])
        key = world.stack / "tls" / "key.pem"
        key.rename(world.stack / "key.pem")
        key.symlink_to(world.stack / "key.pem")
        world.rootless()
        result = world.run("init.sh")
        assert result.returncode != 0
        assert "tls/key.pem is a symbolic link, which the helper never follows" in result.stderr
        assert not [line for line in world.nsenter_log() if "key.pem" in line]
