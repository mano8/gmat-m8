"""The edge's compose services, networks and Caddy files, read without Docker (ADR-0034, M13.3).

Who joins the tunnel network, what the connector may do, which files each container mounts, and
how the Caddyfile picks a mode. Caddy's own reading of the same files is ``tests/live``.
"""

import re
from pathlib import Path
from typing import Any

import pytest

from tests.unit.docker.test_compose import STACK, compose
from tests.unit.docker.test_ops_checks import checks

CADDY = STACK / "caddy"
CADDYFILE = (CADDY / "Caddyfile").read_text(encoding="utf-8")
SNIPPETS = {"tls": {"edge-tls-global", "edge-tls-site"}, "proxy": {"edge-proxy-servers",
            "edge-proxy-site", "edge-proxy-upstream"}}  # fmt: skip


@pytest.fixture(scope="module")
def base() -> dict[str, Any]:
    return compose("docker-compose.yml")


def _members(base: dict[str, Any], network: str) -> set[str]:
    return {
        name for name, service in base["services"].items() if network in service.get("networks", [])
    }


def _binds(service: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        volume["source"]: volume
        for volume in service.get("volumes", [])
        if isinstance(volume, dict)
    }


class TestTheTunnelNetwork:
    def test_only_caddy_and_the_connector_join_it(self, base: dict[str, Any]) -> None:
        assert _members(base, "tunnel") == {"caddy", "cloudflared"}
        assert _members(base, "tunnel_egress") == {"cloudflared"}
        assert base["networks"]["tunnel"]["internal"] is True
        assert "internal" not in base["networks"]["tunnel_egress"]

    def test_the_connector_shares_no_network_with_anything_else(self, base: dict[str, Any]) -> None:
        connector = set(base["services"]["cloudflared"]["networks"])
        for name, service in base["services"].items():
            if name not in {"cloudflared", "caddy"}:
                assert not connector & set(service.get("networks", [])), name
        assert connector & set(base["services"]["caddy"]["networks"]) == {"tunnel"}

    def test_caddy_takes_an_address_of_the_range_and_the_connector_its_own(
        self, base: dict[str, Any]
    ) -> None:
        """Caddy, or a one-off of it, could otherwise take the address it believes."""
        assert base["networks"]["tunnel"]["ipam"]["config"] == [
            {
                "subnet": "${EDGE_TUNNEL_SUBNET:-172.30.90.0/24}",
                "ip_range": "${EDGE_TUNNEL_RANGE:-172.30.90.0/25}",
            }
        ]
        assert base["services"]["caddy"]["networks"] == ["ingress", "edge", "tunnel"]
        connector = base["services"]["cloudflared"]["networks"]
        assert connector == {
            "tunnel": {"ipv4_address": "${EDGE_TUNNEL_ADDRESS:-172.30.90.250}"},
            "tunnel_egress": {},
        }

    def test_one_key_sets_the_connectors_address_and_caddys_trust(
        self, base: dict[str, Any]
    ) -> None:
        caddy = base["services"]["caddy"]["environment"]["EDGE_TUNNEL_ADDRESS"]
        assert caddy == base["services"]["cloudflared"]["networks"]["tunnel"]["ipv4_address"]
        trusted = (CADDY / "edge" / "proxy-cloudflare.caddy").read_text(encoding="utf-8")
        assert 'trusted_proxies static "{$EDGE_TUNNEL_ADDRESS}/32"' in trusted
        assert re.findall(r"^\s*(trusted_proxies|client_ip_headers)\s.*$", trusted, re.M) == [
            "trusted_proxies",
            "client_ip_headers",
        ]
        assert "client_ip_headers Cf-Connecting-Ip\n" in trusted

    def test_the_restore_drill_closes_the_connectors_way_out(self) -> None:
        drill = compose("docker-compose.restore-drill.yml")["networks"]
        assert drill["tunnel"] == {"internal": True}
        assert drill["tunnel_egress"] == {"internal": True}


class TestTheConnector:
    @pytest.fixture
    def connector(self, base: dict[str, Any]) -> dict[str, Any]:
        service: dict[str, Any] = base["services"]["cloudflared"]
        return service

    def test_runs_only_in_its_profile(self, connector: dict[str, Any]) -> None:
        assert connector["profiles"] == ["tunnel"]
        assert checks.TUNNEL_PROFILE == "tunnel"

    def test_is_pinned_and_hardened(self, connector: dict[str, Any]) -> None:
        assert re.fullmatch(
            r"cloudflare/cloudflared:\d{4}\.\d+\.\d+@sha256:[0-9a-f]{64}", connector["image"]
        )
        assert connector["user"] == "1000:1000"
        assert connector["read_only"] is True
        assert connector["cap_drop"] == ["ALL"]
        assert "cap_add" not in connector
        assert connector["security_opt"] == ["no-new-privileges:true"]
        assert connector["mem_limit"] and connector["pids_limit"]
        assert connector["logging"]["options"]["max-size"]
        assert "ports" not in connector and "expose" not in connector

    def test_reads_its_token_from_a_file_only(self, connector: dict[str, Any]) -> None:
        """--token and TUNNEL_TOKEN would win over the file, and print in compose's config."""
        assert connector["environment"] == {"TUNNEL_TOKEN_FILE": "/etc/gmat/tunnel/token"}
        assert connector["command"] == [
            "tunnel", "--no-autoupdate", "--metrics", "127.0.0.1:20241", "run",
        ]  # fmt: skip
        assert "env_file" not in connector
        for name in ("docker-compose.yml", "docker-compose.production.yml", ".env.example"):
            assert "TUNNEL_TOKEN=" not in (STACK / name).read_text(encoding="utf-8")
            assert "TUNNEL_TOKEN:" not in (STACK / name).read_text(encoding="utf-8")

    def test_mounts_only_its_own_directory(self, connector: dict[str, Any]) -> None:
        assert connector["volumes"] == [
            {
                "type": "bind",
                "source": "./tunnel",
                "target": "/etc/gmat/tunnel",
                "read_only": True,
                "bind": {"create_host_path": False},
            }
        ]


class TestTheFilesEachContainerReads:
    def test_only_caddy_mounts_the_tls_key_and_only_the_connector_the_token(
        self, base: dict[str, Any]
    ) -> None:
        mounts = {name: set(_binds(service)) for name, service in base["services"].items()}
        assert {name for name, sources in mounts.items() if "./tls" in sources} == {"caddy"}
        assert {name for name, sources in mounts.items() if "./tunnel" in sources} == {
            "cloudflared"
        }

    def test_caddys_tls_mount_is_read_only_and_never_made_by_docker(
        self, base: dict[str, Any]
    ) -> None:
        caddy = base["services"]["caddy"]
        assert _binds(caddy)["./tls"] == {
            "type": "bind",
            "source": "./tls",
            "target": "/etc/caddy/tls",
            "read_only": True,
            "bind": {"create_host_path": False},
        }
        assert "./caddy/edge:/etc/caddy/edge:ro" in caddy["volumes"]

    def test_neither_directory_is_ever_committed(self) -> None:
        ignored = (STACK / ".gitignore").read_text(encoding="utf-8").splitlines()
        assert {"tls/", "tunnel/"} <= set(ignored)


class TestTheModesInCaddy:
    def test_compose_gives_each_mode_its_default(self, base: dict[str, Any]) -> None:
        """A set-but-empty variable would not take Caddy's own default (measured)."""
        environment = base["services"]["caddy"]["environment"]
        assert environment["EDGE_TLS"] == "${EDGE_TLS:-acme}"
        assert environment["EDGE_PROXY"] == "${EDGE_PROXY:-none}"
        assert environment["EDGE_EXTRA_HOST"] == "${EDGE_EXTRA_HOST:-}"

    def test_the_caddyfile_imports_one_file_per_kind_before_anything(self) -> None:
        first_block = CADDYFILE.index("\n{\n")
        assert re.findall(r"^import edge/.*$", CADDYFILE[:first_block], re.M) == [
            "import edge/tls-{$EDGE_TLS}.caddy",
            "import edge/proxy-{$EDGE_PROXY}.caddy",
        ]
        assert len(re.findall(r"^\s*import edge/", CADDYFILE, re.M)) == 2

    @pytest.mark.parametrize("path", sorted((CADDY / "edge").glob("*.caddy")), ids=lambda p: p.name)
    def test_each_file_defines_its_kinds_snippets_and_nothing_else(self, path: Path) -> None:
        """So a value that matches two files declares them twice, and Caddy refuses it."""
        text = path.read_text(encoding="utf-8")
        kind = path.name.split("-", 1)[0]
        assert set(re.findall(r"^\((\S+)\) \{$", text, re.M)) == SNIPPETS[kind]
        top_level = [line for line in text.splitlines() if line and not line[0].isspace()]
        assert all(line.startswith(("#", "(", "}")) for line in top_level), top_level

    def test_each_snippet_is_imported_where_it_applies(self) -> None:
        for name in set().union(*SNIPPETS.values()):
            assert len(re.findall(rf"^\s*import {name}$", CADDYFILE, re.M)) == 1, name
        assert len(re.findall(r"^\s*import web-forwarded$", CADDYFILE, re.M)) == 2

    def test_every_mode_writes_the_address_django_reads(self) -> None:
        block = CADDYFILE[CADDYFILE.index("(web-forwarded) {") :]
        block = block[: block.index("\n}\n")]
        assert "header_up X-Forwarded-For {client_ip}" in block
        assert "header_up -Cf-Connecting-Ip" in block
        assert CADDYFILE.count("reverse_proxy web:8000 {") == 2

    def test_the_tunnel_mode_takes_nothing_else_from_the_connector(self) -> None:
        text = (CADDY / "edge" / "proxy-cloudflare.caddy").read_text(encoding="utf-8")
        assert "header_up X-Forwarded-Proto https" in text
        assert "header_up -X-Forwarded-Host" in text

    def test_the_tunnel_mode_asks_cloudflare_not_to_rewrite_after_compression(self) -> None:
        text = (CADDY / "edge" / "proxy-cloudflare.caddy").read_text(encoding="utf-8")
        assert re.search(r'header \{\n\t\t\+Cache-Control "no-transform"\n\t\tdefer\n\t\}', text)

    def test_files_mode_asks_acme_for_nothing(self) -> None:
        text = (CADDY / "edge" / "tls-files.caddy").read_text(encoding="utf-8")
        assert "auto_https disable_certs" in text
        assert "default_sni {$APP_DOMAIN}" in text
        assert "tls /etc/caddy/tls/cert.pem /etc/caddy/tls/key.pem" in text

    def test_both_logs_drop_access_token_of_cloudflare(self) -> None:
        assert CADDYFILE.count("request>headers>Cf-Access-Jwt-Assertion delete") == 2
        assert CADDYFILE.count("format filter {") == 2

    def test_the_extra_host_is_one_more_address_of_the_site(self) -> None:
        assert "{$APP_DOMAIN:localhost}:{$HTTPS_PORT:8443} {$EDGE_EXTRA_HOST} {" in CADDYFILE
        assert CADDYFILE.count("{$EDGE_EXTRA_HOST}") == 1


class TestTheTemplate:
    def test_documents_every_key_off_by_default(self) -> None:
        template = checks.read_env(STACK / ".env.example")
        assert template["EDGE_TLS"] == "acme"
        assert template["EDGE_PROXY"] == "none"
        assert template["EDGE_EXTRA_HOST"] == ""
        assert template["COMPOSE_PROFILES"] == ""
        for key in ("EDGE_TUNNEL_SUBNET", "EDGE_TUNNEL_RANGE", "EDGE_TUNNEL_ADDRESS"):
            assert template[key] == checks.EDGE_DEFAULTS[key], key
