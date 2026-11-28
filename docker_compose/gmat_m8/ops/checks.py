#!/usr/bin/env python3
"""What deploy.sh, recover.sh and restore-drill.sh read from JSON (plan §9).

Standard library only: this runs on the host, before any image is trusted (Debian ships
python3). Each command prints plain text for the shell and exits non-zero on a mismatch.

  asset RELEASE NAME        the download URL of one asset of a GitHub release (JSON file)
  manifest FILE --version V --repository OWNER/NAME --image-prefix PREFIX
                            validates a verified release-manifest.json; prints KEY=VALUE lines
  states MANIFEST KEY       exit 0 when the signed manifest states this about its migrations
  admin-cidrs VALUE [--enabled DJANGO_ADMIN_ENABLED]
                            refuses an allowlist that is open or holds Docker's bridge ranges,
                            while the admin is on (without --enabled it is taken as on)
  dump-version MANIFEST     the app version a backup manifest was taken with
  dump-time MANIFEST        when a backup was taken (a restore's loss window starts there)
  compare-counts MANIFEST RESTORED
                            the restored row counts against a backup manifest
  cosign-version OUTPUT     the version ``cosign version`` printed, refused below cosign 3
  port-driver FILE --from api|cmdline
                            the rootless daemon's port driver, from RootlessKit's API answer or
                            its NUL-separated command line; refused unless it keeps the
                            visitor's address (exit 2), exit 3 when the input names none
  published-ports CONFIG --floor N
                            the host ports ``docker compose config --format json`` publishes;
                            refused when one is below N, a rootless daemon's floor (exit 2)

A file named ``-`` is read from stdin: the scripts pipe a backup manifest from the helper that
alone reads backups/ (ADR-0033), so this never opens a file there.
"""

import argparse
import ipaddress
import json
import re
import sys
from pathlib import Path
from typing import Any

VERSION = re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z][0-9A-Za-z.-]*)?$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
SHA = re.compile(r"^[0-9a-f]{40}$")
# Docker's default address pools: behind its proxy, a client can appear to come from one of
# them, so an allowlist holding one lets everybody in (exception X10, docs/security.md).
DOCKER_RANGES = tuple(
    ipaddress.ip_network(network) for network in ("172.16.0.0/12", "192.168.0.0/16")
)
# The widest network an allowlist may name, by IP version: an office, a VPN, a provider's block.
WIDEST_PREFIX = {4: 8, 6: 32}
# DJANGO_ADMIN_ENABLED as config.env.env_bool reads it (empty: production's default, off).
ADMIN_ON = frozenset({"1", "true", "yes", "on"})
ADMIN_OFF = frozenset({"", "0", "false", "no", "off"})
IMAGE_NAMES = {"app": "gmat-m8", "backup": "gmat-m8-backup"}
MIGRATION_KEYS = ("irreversible", "old_code_works")
# cosign 2 cannot read the signatures docker-publish makes with cosign 3 ("no signatures found";
# M11). Moves with the runbook's minimum (vps-setup.md §4.3) when the installer's cosign does.
COSIGN_MIN_MAJOR = 3
GIT_VERSION = re.compile(r"^GitVersion:[ \t]+(v?(\d+)\.\d+\.\d+\S*)[ \t]*$", re.MULTILINE)
# The port driver ADR-0033 point 4 accepts: it hands the visitor's address to the containers, for
# TCP and UDP alike (measured in M12.5). ``builtin`` hid it there with Docker's defaults; the
# others are unmeasured, experimental or hide it.
KEEPS_ADDRESS = frozenset({"slirp4netns"})
# Drivers that keep the address too but that the maintainer refused (M12.5), and why.
NOT_CHOSEN = {
    "implicit": "keeps the visitors' addresses, but ADR-0033 accepts slirp4netns alone: pasta is "
    "experimental in Docker, was slower when measured, and publishes a port only on every host "
    "address",
}
PORT_DRIVER_OPTION = "--port-driver"


class Refused(Exception):
    """The input is not what the caller may go on with."""


class Unknown(Exception):
    """The input does not say what the caller asked (exit 3: a warning, not a refusal)."""


def _load(path: str) -> Any:
    text = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    return json.loads(text)


def asset_url(release: dict[str, Any], name: str) -> str:
    for asset in release.get("assets") or []:
        if asset.get("name") == name and asset.get("url"):
            return str(asset["url"])
    raise Refused(f"the release has no asset {name}")


def migration_statements(manifest: dict[str, Any]) -> dict[str, str]:
    """What a release states about its migrations. Only the signed manifest counts, never the
    release notes (ADR-0017); one that does not say is refused, not read as "no"."""
    statements = manifest.get("migration")
    if not isinstance(statements, dict):
        raise Refused("the manifest does not state its migrations")
    for key in MIGRATION_KEYS:
        if not isinstance(statements.get(key), str):
            raise Refused(f"the manifest does not state {key}")
    return statements


def _check_image(manifest: dict[str, Any], key: str, prefix: str) -> dict[str, str]:
    image = (manifest.get("images") or {}).get(key) or {}
    repository = f"{prefix}/{IMAGE_NAMES[key]}"
    digest = str(image.get("digest", ""))
    if image.get("repository") != repository:
        raise Refused(f"{key}: the manifest names {image.get('repository')!r}, not {repository!r}")
    if not DIGEST.match(digest):
        raise Refused(f"{key}: malformed digest {digest!r}")
    if image.get("ref") != f"{repository}@{digest}":
        raise Refused(f"{key}: the reference does not match the digest")
    return {"digest": digest, "ref": f"{repository}@{digest}"}


def check_manifest(
    manifest: dict[str, Any], version: str, repository: str, prefix: str
) -> dict[str, str]:
    """The values deploy.sh may use; anything that differs from the request is refused."""
    expected = {
        "schema_version": 1,
        "version": version,
        "tag": f"v{version}",
        "repository": repository,
    }
    for key, value in expected.items():
        if manifest.get(key) != value:
            raise Refused(f"manifest {key} is {manifest.get(key)!r}, expected {value!r}")
    commit = str(manifest.get("commit", ""))
    if not SHA.match(commit):
        raise Refused(f"malformed commit {commit!r}")
    app = _check_image(manifest, "app", prefix)
    backup = _check_image(manifest, "backup", prefix)
    statements = migration_statements(manifest)
    return {
        "RELEASE_IRREVERSIBLE": "1" if statements["irreversible"] else "0",
        "RELEASE_COMMIT": commit,
        "RELEASE_APP_DIGEST": app["digest"],
        "RELEASE_APP_REF": app["ref"],
        "RELEASE_BACKUP_DIGEST": backup["digest"],
        "RELEASE_BACKUP_REF": backup["ref"],
    }


def admin_enabled(value: str) -> bool:
    """Whether ``web`` will serve the admin; a word Django would refuse is refused here."""
    word = value.strip().lower()
    if word not in ADMIN_ON | ADMIN_OFF:
        raise Refused(
            f"DJANGO_ADMIN_ENABLED: {value!r} is not one of {sorted(ADMIN_ON | ADMIN_OFF)}"
        )
    return word in ADMIN_ON


def check_admin_cidrs(value: str) -> None:
    """Production names the networks that reach the admin, and never a bridge range. Read as
    networks, not strings: ``0::/0`` and ``::/1`` are as open as ``::/0``."""
    entries = value.split()
    if not entries:
        raise Refused("ADMIN_ALLOWED_CIDRS is empty")
    for entry in entries:
        try:
            network = ipaddress.ip_network(entry, strict=False)
        except ValueError as exc:
            raise Refused(f"ADMIN_ALLOWED_CIDRS: {entry!r} is not a network") from exc
        if network.prefixlen < WIDEST_PREFIX[network.version]:
            raise Refused(f"ADMIN_ALLOWED_CIDRS: {entry} is open to every network, or near it")
        overlap = any(
            network.version == docker.version and network.overlaps(docker)
            for docker in DOCKER_RANGES
        )
        if overlap:
            raise Refused(f"ADMIN_ALLOWED_CIDRS: {entry} overlaps Docker's bridge ranges")


def count_differences(manifest: dict[str, Any], restored: dict[str, Any]) -> list[str]:
    excluded = set(manifest.get("excluded_data_tables") or [])
    wanted = manifest.get("row_counts") or {}
    found = restored.get("row_counts") or {}
    return [
        f"{table}: manifest {count}, restored {found.get(table, 'missing')}"
        for table, count in sorted(wanted.items())
        if table not in excluded and found.get(table) != count
    ]


def cosign_version(text: str) -> str:
    """The version ``cosign version`` printed, if its major is at least COSIGN_MIN_MAJOR."""
    match = GIT_VERSION.search(text)
    if not match:
        raise Refused("cosign version printed no GitVersion")
    if int(match[2]) < COSIGN_MIN_MAJOR:
        raise Refused(f"this is cosign {match[1]}")
    return match[1]


def port_driver_from_info(info: Any) -> tuple[str, list[str] | None]:
    """The driver and protocols of RootlessKit's ``GET /v1/info`` answer (``portDriver``)."""
    port = info.get("portDriver") if isinstance(info, dict) else None
    if not isinstance(port, dict) or not isinstance(port.get("driver"), str) or not port["driver"]:
        raise Unknown("RootlessKit's API names no port driver")
    protos = port.get("protos")
    return port["driver"], [str(proto) for proto in protos] if isinstance(protos, list) else None


def port_driver_from_cmdline(text: str) -> tuple[str, None]:
    """The ``--port-driver`` RootlessKit was started with, from its NUL-separated command line.

    Repeated, the last one counts, as RootlessKit reads it (seen on RootlessKit 3.3.0). The
    command it runs starts after its own options; dockerd has no such option and would not
    start with one, so reading on past it finds nothing more.
    """
    driver = ""
    arguments = text.split("\0")
    for index, argument in enumerate(arguments):
        if argument == "--":
            break
        if argument.startswith(f"{PORT_DRIVER_OPTION}="):
            driver = argument.split("=", 1)[1]
        elif argument == PORT_DRIVER_OPTION and index + 1 < len(arguments):
            driver = arguments[index + 1]
    if not driver:
        raise Unknown("RootlessKit's command line names no port driver")
    return driver, None


def check_port_driver(driver: str, protos: list[str] | None) -> str:
    """The driver as the scripts print it, if it keeps the visitor's address (ADR-0033)."""
    stated = f"{driver} ({', '.join(protos)})" if protos else driver
    if driver in NOT_CHOSEN:
        raise Refused(f"the port driver {stated} {NOT_CHOSEN[driver]}")
    if driver not in KEEPS_ADDRESS:
        raise Refused(
            f"the port driver {stated} hides the visitors' addresses: allauth's per-address "
            "rate limits would count every visitor as one, so one attacker's failed sign-ins "
            "would lock everyone out, and the audit log's addresses would mean nothing"
        )
    if protos is not None and "udp" not in protos:
        raise Refused(
            f"the port driver {stated} does not carry UDP: the production overlay publishes "
            "443/udp (HTTP/3)"
        )
    return stated


def published_ports(config: Any) -> list[tuple[int, str, str]]:
    """Each host port the merged compose files publish: (port, protocol, service), lowest first.
    A range counts from its first port."""
    services = config.get("services") if isinstance(config, dict) else None
    if not isinstance(services, dict):
        raise Refused("docker compose config names no services")
    found = []
    for name, service in services.items():
        for port in (service or {}).get("ports") or []:
            # One without a number is the daemon's choice, far above any floor.
            published = str(port.get("published") or "")
            if published:
                found.append((int(published.split("-")[0]), str(port.get("protocol", "tcp")), name))
    return sorted(found)


def check_published_ports(config: Any, floor: int) -> str:
    """The published ports as the scripts print them, if a rootless daemon may publish each one:
    below the floor RootlessKit refuses, and with slirp4netns without saying why (M12.5)."""
    ports = published_ports(config)
    if ports and ports[0][0] < floor:  # the lowest decides
        port, protocol, service = ports[0]
        raise Refused(
            f"the rootless daemon cannot publish {service}'s port {port}/{protocol}: "
            f"net.ipv4.ip_unprivileged_port_start is {floor}"
        )
    return ", ".join(f"{port}/{protocol}" for port, protocol, _ in ports) or "none"


def _asset(args: argparse.Namespace) -> str:
    return asset_url(_load(args.release), args.name)


def _states(args: argparse.Namespace) -> str:
    if not migration_statements(_load(args.manifest))[args.key]:
        raise SystemExit(1)
    return ""


def _manifest(args: argparse.Namespace) -> str:
    if not VERSION.match(args.version):
        raise Refused(f"not a release version: {args.version}")
    values = check_manifest(_load(args.file), args.version, args.repository, args.image_prefix)
    return "\n".join(f"{key}={value}" for key, value in values.items())


def _admin_cidrs(args: argparse.Namespace) -> str:
    """The allowlist matters only while the admin is on (ADR-0030): off, it has no URL."""
    if args.enabled is not None and not admin_enabled(args.enabled):
        return "the admin is off: no allowlist needed"
    check_admin_cidrs(args.value)
    return ""


def _dump_version(args: argparse.Namespace) -> str:
    return str(_load(args.manifest).get("app_version") or "")


def _dump_time(args: argparse.Namespace) -> str:
    created = _load(args.manifest).get("created_at")
    if not created:
        raise Refused("the manifest does not say when it was taken")
    return str(created)


def _compare_counts(args: argparse.Namespace) -> str:
    differences = count_differences(_load(args.manifest), _load(args.restored))
    if differences:
        raise Refused("; ".join(differences))
    return ""


def _cosign_version(args: argparse.Namespace) -> str:
    text = sys.stdin.read() if args.output == "-" else Path(args.output).read_text(encoding="utf-8")
    return cosign_version(text)


def _port_driver(args: argparse.Namespace) -> str:
    if args.source == "cmdline":
        raw = sys.stdin.buffer.read() if args.file == "-" else Path(args.file).read_bytes()
        return check_port_driver(*port_driver_from_cmdline(raw.decode("utf-8", "replace")))
    try:
        info = _load(args.file)
    except ValueError as exc:
        raise Unknown(f"RootlessKit's API answered something else than JSON: {exc}") from exc
    return check_port_driver(*port_driver_from_info(info))


def _published_ports(args: argparse.Namespace) -> str:
    return check_published_ports(_load(args.config), args.floor)


COMMANDS = {
    "asset": _asset,
    "manifest": _manifest,
    "states": _states,
    "admin-cidrs": _admin_cidrs,
    "dump-version": _dump_version,
    "dump-time": _dump_time,
    "compare-counts": _compare_counts,
    "cosign-version": _cosign_version,
    "port-driver": _port_driver,
    "published-ports": _published_ports,
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    asset = commands.add_parser("asset")
    asset.add_argument("release")
    asset.add_argument("name")
    manifest = commands.add_parser("manifest")
    manifest.add_argument("file")
    manifest.add_argument("--version", required=True)
    manifest.add_argument("--repository", required=True)
    manifest.add_argument("--image-prefix", required=True)
    states = commands.add_parser("states")
    states.add_argument("manifest")
    states.add_argument("key", choices=MIGRATION_KEYS)
    cidrs = commands.add_parser("admin-cidrs")
    cidrs.add_argument("value")
    cidrs.add_argument("--enabled")
    commands.add_parser("dump-version").add_argument("manifest")
    commands.add_parser("dump-time").add_argument("manifest")
    counts = commands.add_parser("compare-counts")
    counts.add_argument("manifest")
    counts.add_argument("restored")
    commands.add_parser("cosign-version").add_argument("output")
    driver = commands.add_parser("port-driver")
    driver.add_argument("file")
    driver.add_argument("--from", dest="source", choices=("api", "cmdline"), required=True)
    ports = commands.add_parser("published-ports")
    ports.add_argument("config")
    ports.add_argument("--floor", type=int, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        output = COMMANDS[args.command](args)
    except Unknown as exc:
        print(exc, file=sys.stderr)
        return 3
    except (Refused, ValueError, OSError) as exc:
        print(exc, file=sys.stderr)
        return 2
    if output:
        print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
