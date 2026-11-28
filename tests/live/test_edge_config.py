"""Caddy's own reading of the edge modes (ADR-0034, M13.3), with the pinned image and no stack.

Without the edge keys, Caddy's configuration is what it was before they existed, apart from what
every mode adds: the two ``header_up`` lines of each proxy and the Access token in the log
filters. "Before" is the same Caddyfile with every edge construct taken out, so this keeps holding
as the Caddyfile changes; a mode file that adds anything by default fails it.

Needs ``docker`` (it runs ``caddy adapt`` in a container without a network). Skips without it,
unless ``LIVE_REQUIRED=1``.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.live

ROOT = Path(__file__).resolve().parents[2]
STACK = ROOT / "docker_compose" / "gmat_m8"
# What compose gives Caddy without the keys: the base file, and the production overlay's values.
LOCAL = {
    "APP_DOMAIN": "localhost",
    "HTTP_PORT": "8080",
    "HTTPS_PORT": "8443",
    "HTTPS_PORT_SUFFIX": ":8443",
    "ALT_SVC_PORT": "8443",
    "HTTP_PROTOCOLS": "h1 h2",
    "DJANGO_ADMIN_ENABLED": "false",
    "ADMIN_URL_PATH": "admin",
    "ADMIN_ALLOWED_CIDRS": "0.0.0.0/0 ::/0",
}
PRODUCTION = {
    **LOCAL,
    "APP_DOMAIN": "gmat.example.org",
    "ACME_EMAIL": "ops@example.org",
    "HTTPS_PORT_SUFFIX": "",
    "ALT_SVC_PORT": "443",
    "HTTP_PROTOCOLS": "h1 h2 h3",
    "DJANGO_ADMIN_ENABLED": "true",
    "ADMIN_ALLOWED_CIDRS": "203.0.113.0/24",
}
DEFAULTS = {"EDGE_TLS": "acme", "EDGE_PROXY": "none", "EDGE_EXTRA_HOST": ""}
EDGE_LINE = re.compile(
    r"^\s*(import (edge/|edge-|web-forwarded)\S*|request>headers>Cf-Access-Jwt-Assertion delete)$"
)
ACCESS_TOKEN = "request>headers>Cf-Access-Jwt-Assertion"
COMMENT = re.compile(r"^\s*#")


def caddy_image() -> str:
    text = (STACK / "docker-compose.yml").read_text(encoding="utf-8")
    found = re.search(r"^  caddy:\n(?:    .*\n)*?    image: (\S+)$", text, re.M)
    assert found, "the compose file names no Caddy image"
    return found[1]


@pytest.fixture(scope="module")
def docker() -> str:
    found = shutil.which("docker")
    answered = found and subprocess.run(  # noqa: S603 (docker info, nothing from input)
        [found, "info"], capture_output=True, check=False
    )
    if found and answered and answered.returncode == 0:
        return found
    if os.environ.get("LIVE_REQUIRED") == "1":
        pytest.fail("docker does not answer")
    pytest.skip("docker does not answer")


def adapt(docker: str, directory: Path, environment: dict[str, str]) -> dict[str, Any]:
    variables = [part for key, value in environment.items() for part in ("-e", f"{key}={value}")]
    result = subprocess.run(  # noqa: S603 (fixed docker invocation, test input only)
        [docker, "run", "--rm", "--network", "none", *variables,
         "-v", f"{directory}:/etc/caddy:ro", caddy_image(),
         "caddy", "adapt", "--config", "/etc/caddy/Caddyfile"],
        capture_output=True, text=True, timeout=120, check=False,
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    config: dict[str, Any] = json.loads(result.stdout)
    return config


def without_edge(caddyfile: str) -> str:
    """The Caddyfile as it would be had the edge modes never existed."""
    text = re.sub(r"^\(web-forwarded\) \{\n.*?^\}\n", "", caddyfile, flags=re.M | re.S)
    text = text.replace(" {$EDGE_EXTRA_HOST} {", " {")
    # Comments change nothing Caddy reads; without them, what is left names the edge nowhere.
    lines = text.splitlines(keepends=True)
    return "".join(line for line in lines if not EDGE_LINE.match(line) and not COMMENT.match(line))


def _each_dict(node: Any) -> list[dict[str, Any]]:
    if isinstance(node, dict):
        return [node, *(found for value in node.values() for found in _each_dict(value))]
    if isinstance(node, list):
        return [found for value in node for found in _each_dict(value)]
    return []


def drop_every_modes_additions(config: dict[str, Any]) -> int:
    """Takes out what every mode adds, asserting each is as written; returns how many."""
    removed = 0
    for node in _each_dict(config):
        if node.get("handler") == "reverse_proxy":
            request = node["headers"]["request"]
            assert request["set"].pop("X-Forwarded-For") == ["{http.vars.client_ip}"]
            assert request.pop("delete") == ["Cf-Connecting-Ip"]
            removed += 1
        fields = node.get("fields")
        if isinstance(fields, dict) and ACCESS_TOKEN in fields:
            assert fields.pop(ACCESS_TOKEN) == {"filter": "delete"}
            removed += 1
    return removed


@pytest.mark.parametrize("environment", [LOCAL, PRODUCTION], ids=["local", "production"])
def test_without_the_keys_caddy_reads_what_it_read_before(
    docker: str, tmp_path: Path, environment: dict[str, str]
) -> None:
    before = tmp_path / "before"
    before.mkdir()
    (before / "Caddyfile").write_text(
        without_edge((STACK / "caddy" / "Caddyfile").read_text(encoding="utf-8")),
        encoding="utf-8",
    )
    stripped = (before / "Caddyfile").read_text(encoding="utf-8")
    assert "import edge" not in stripped and "{$EDGE_" not in stripped
    assert "web-forwarded" not in stripped and "Cf-" not in stripped
    now = adapt(docker, STACK / "caddy", {**environment, **DEFAULTS})
    # Two proxies and two log filters: anything else that differs fails the comparison.
    assert drop_every_modes_additions(now) == 4
    assert now == adapt(docker, before, environment)
