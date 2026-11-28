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
  edge --env .env --app-env app.env
                            the edge settings and their files (ADR-0034): a mode outside the
                            allowlist, a trust wider than the connector's one address, a
                            certificate that does not name the site, a host Django would refuse;
                            prints the modes, warnings, and the host the links in email use

A file named ``-`` is read from stdin: the scripts pipe a backup manifest from the helper that
alone reads backups/ (ADR-0033), so this never opens a file there.
"""

import _ssl
import argparse
import ipaddress
import json
import re
import ssl
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

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
# The edge (ADR-0034): each mode's allowed values, the default first; what compose gives a key that
# is absent or empty (``${KEY:-default}``), which is what Caddy and Docker then read.
EDGE_MODES = {"EDGE_TLS": ("acme", "files"), "EDGE_PROXY": ("none", "cloudflare")}
EDGE_DEFAULTS = {
    "APP_DOMAIN": "localhost",
    "EDGE_TLS": "acme",
    "EDGE_PROXY": "none",
    "EDGE_SUBNET": "172.30.80.0/24",
    "EDGE_TUNNEL_SUBNET": "172.30.90.0/24",
    "EDGE_TUNNEL_RANGE": "172.30.90.0/25",
    "EDGE_TUNNEL_ADDRESS": "172.30.90.250",
}
TUNNEL_PROFILE = "tunnel"
# The tunnel network's subnet: wide enough for its dynamic range, narrow like every other one.
TUNNEL_PREFIXES = (16, 28)
# Caddy and a one-off of it at once, with room to spare: Docker gives each a new address.
TUNNEL_RANGE_MINIMUM = 8
CERTIFICATE_WARNING_DAYS = 30
ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
HOST_LABEL = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)$")
SAN_KINDS = ("DNS", "IP Address")
# How the ssl module decodes a peer's certificate, from a file: CPython's own tests use it, and the
# standard library has no public way to read a certificate's names without a handshake.
DECODE_CERTIFICATE = getattr(_ssl, "_test_decode_cert", None)


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


# --- the edge (ADR-0034) ------------------------------------------------------------------------


def read_env(path: Path) -> dict[str, str]:
    """``KEY=value`` lines as ``env_value`` reads them (ops/common.sh): the last one wins, nothing
    is unquoted, so a quoted value is refused rather than read as compose would."""
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and ENV_KEY.match(key):
            values[key] = value
    return values


def _setting(env: dict[str, str], key: str) -> str:
    return env.get(key) or EDGE_DEFAULTS[key]


def edge_mode(env: dict[str, str], key: str) -> str:
    """A mode spelled exactly as one of its files is named: never a glob, a case or a list."""
    value = _setting(env, key)
    if value not in EDGE_MODES[key]:
        raise Refused(f"{key}: {value!r} is not one of {', '.join(EDGE_MODES[key])}")
    return value


def check_host(key: str, value: str) -> str:
    """One host name or IPv4 address, which Caddy reads as one site address and Django as one
    host: no port, scheme, path, wildcard, space or list. IPv6 needs brackets that the two write
    differently, so it is refused."""
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        labels = value.split(".")
        if len(value) > 253 or not all(HOST_LABEL.match(label) for label in labels):
            raise Refused(
                f"{key}: {value!r} is not one host name (no port, scheme, wildcard or space)"
            ) from None
        return value.lower()
    if address.version == 6:
        raise Refused(f"{key}: {value} is an IPv6 address: give the host a name")
    return str(address)


def edge_hosts(env: dict[str, str]) -> list[str]:
    hosts = [check_host("APP_DOMAIN", _setting(env, "APP_DOMAIN"))]
    if env.get("EDGE_EXTRA_HOST"):
        extra = check_host("EDGE_EXTRA_HOST", env["EDGE_EXTRA_HOST"])
        if extra == hosts[0]:
            raise Refused(f"EDGE_EXTRA_HOST: {extra} is APP_DOMAIN already")
        hosts.append(extra)
    return hosts


def _ipv4_network(key: str, value: str) -> ipaddress.IPv4Network:
    try:
        network = ipaddress.ip_network(value)  # strict: an address with host bits is refused
    except ValueError as exc:
        raise Refused(
            f"{key}: {value!r} is not a network (address/prefix, host bits zero)"
        ) from exc
    if not isinstance(network, ipaddress.IPv4Network):
        raise Refused(f"{key}: {value} is not an IPv4 network")
    return network


def tunnel_address(env: dict[str, str]) -> ipaddress.IPv4Address:
    """The one address Caddy believes, written as an address only: Caddy appends ``/32``."""
    value = _setting(env, "EDGE_TUNNEL_ADDRESS")
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise Refused(
            f"EDGE_TUNNEL_ADDRESS: {value!r} is not one address "
            "(never a range, a list or a keyword)"
        ) from exc
    if not isinstance(address, ipaddress.IPv4Address):
        raise Refused(
            f"EDGE_TUNNEL_ADDRESS: {value} is IPv6, where Caddy's /32 would be 2^96 addresses"
        )
    return address


def check_tunnel_network(env: dict[str, str]) -> ipaddress.IPv4Network:
    """The tunnel network exists in every mode (Caddy joins it), so its keys always count. The
    connector's address is inside its subnet and outside the range Docker gives Caddy (and any
    one-off of it) an address from, and is none of the subnet's own addresses."""
    subnet = _ipv4_network("EDGE_TUNNEL_SUBNET", _setting(env, "EDGE_TUNNEL_SUBNET"))
    dynamic = _ipv4_network("EDGE_TUNNEL_RANGE", _setting(env, "EDGE_TUNNEL_RANGE"))
    if not subnet.is_private or not TUNNEL_PREFIXES[0] <= subnet.prefixlen <= TUNNEL_PREFIXES[1]:
        raise Refused(f"EDGE_TUNNEL_SUBNET: {subnet} is not a private network from /16 to /28")
    if not dynamic.subnet_of(subnet) or dynamic.num_addresses < TUNNEL_RANGE_MINIMUM:
        raise Refused(
            f"EDGE_TUNNEL_RANGE: {dynamic} is not a range of at least {TUNNEL_RANGE_MINIMUM} "
            f"addresses inside EDGE_TUNNEL_SUBNET {subnet}"
        )
    if subnet.overlaps(ipaddress.ip_network(_setting(env, "EDGE_SUBNET"), strict=False)):
        raise Refused(f"EDGE_TUNNEL_SUBNET: {subnet} overlaps EDGE_SUBNET")
    address = tunnel_address(env)
    own = (subnet.network_address, subnet.network_address + 1, subnet.broadcast_address)
    if address not in subnet or address in own:
        raise Refused(
            f"EDGE_TUNNEL_ADDRESS: {address} is not a container's address of {subnet} "
            "(its network, gateway and broadcast addresses are not)"
        )
    if address in dynamic:
        raise Refused(
            f"EDGE_TUNNEL_ADDRESS: {address} is inside EDGE_TUNNEL_RANGE {dynamic}, "
            "where Caddy could take it"
        )
    return subnet


def check_tunnel_mode(env: dict[str, str], tls: str, proxy: str) -> None:
    """The connector's profile with the mode and only with it: without the mode Caddy would see
    every visitor of the tunnel as one address. ACME cannot reach Caddy through the tunnel."""
    profiles = {name.strip() for name in env.get("COMPOSE_PROFILES", "").split(",")}
    if (TUNNEL_PROFILE in profiles) != (proxy == "cloudflare"):
        raise Refused(
            f"COMPOSE_PROFILES holds {TUNNEL_PROFILE!r} if and only if EDGE_PROXY=cloudflare"
        )
    if proxy == "cloudflare" and tls != "files":
        raise Refused("EDGE_PROXY=cloudflare needs EDGE_TLS=files: ACME cannot reach Caddy there")


def allowed_by_django(host: str, patterns: list[str]) -> bool:
    """``django.http.request.validate_host`` without its wildcard, which production refuses."""
    return any(
        pattern == host
        or (pattern.startswith(".") and (host.endswith(pattern) or host == pattern[1:]))
        for pattern in patterns
    )


def _env_list(app_env: dict[str, str], key: str) -> list[str]:
    """``config.env.env_list``: comma-separated, without blanks."""
    return [item.strip().lower() for item in app_env.get(key, "").split(",") if item.strip()]


def check_allowed_hosts(app_env: dict[str, str], hosts: list[str]) -> None:
    patterns = _env_list(app_env, "DJANGO_ALLOWED_HOSTS")
    missing = [host for host in hosts if not allowed_by_django(host, patterns)]
    if missing:
        raise Refused(
            f"DJANGO_ALLOWED_HOSTS in app.env lacks {', '.join(missing)}: Django would refuse "
            "every request Caddy passes on for it"
        )


def check_admin_reach(env: dict[str, str], subnet: ipaddress.IPv4Network, proxy: str) -> None:
    """Through the tunnel every visitor's peer is the connector, which the admin's allowlist
    (``remote_ip``) would then let in, whoever the visitor is."""
    if proxy != "cloudflare" or not admin_enabled(env.get("DJANGO_ADMIN_ENABLED", "")):
        return
    for entry in env.get("ADMIN_ALLOWED_CIDRS", "").split():
        try:
            network = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            continue  # the admin-cidrs check refuses it
        if network.version == 4 and network.overlaps(subnet):
            raise Refused(f"ADMIN_ALLOWED_CIDRS: {entry} holds the tunnel's connector")


def read_certificate(path: Path) -> dict[str, Any]:
    """The first certificate of a PEM file, as the ``ssl`` module decodes a peer's."""
    if path.is_symlink() or not path.is_file():
        raise Refused(f"{path.parent.name}/{path.name} is missing or not a plain file")
    if DECODE_CERTIFICATE is None:
        raise Refused("this python3 cannot decode a certificate (ssl._ssl._test_decode_cert)")
    try:
        decoded: dict[str, Any] = DECODE_CERTIFICATE(str(path))
    except (ssl.SSLError, OSError, ValueError) as exc:
        raise Refused(f"{path.parent.name}/{path.name} is not a PEM certificate: {exc}") from exc
    return decoded


def names_host(certificate: dict[str, Any], host: str) -> bool:
    """A host is named by its own entry, or a name by a wildcard one level up."""
    names = {
        str(value).strip().lower()
        for kind, value in certificate.get("subjectAltName", ())
        if kind in SAN_KINDS
    }
    parent = host.partition(".")[2]
    is_name = not re.fullmatch(r"[0-9.]+", host)
    return host in names or (is_name and bool(parent) and f"*.{parent}" in names)


def check_certificate(directory: Path, hosts: list[str], now: float) -> list[str]:
    """tls/cert.pem names every host of the site and is valid now, with its key beside it (which
    this account may not read: deploy.sh checks its owner through the helper, Caddy reads it)."""
    certificate = read_certificate(directory / "tls" / "cert.pem")
    missing = [host for host in hosts if not names_host(certificate, host)]
    if missing:
        raise Refused(f"tls/cert.pem does not name {', '.join(missing)}")
    if ssl.cert_time_to_seconds(certificate["notBefore"]) > now:
        raise Refused(f"tls/cert.pem is valid only from {certificate['notBefore']}")
    expires = ssl.cert_time_to_seconds(certificate["notAfter"])
    if expires <= now:
        raise Refused(f"tls/cert.pem expired on {certificate['notAfter']}")
    key = directory / "tls" / "key.pem"
    if key.is_symlink() or not key.is_file():
        raise Refused("tls/key.pem is missing or not a plain file")
    days = int((expires - now) // 86400)
    if days < CERTIFICATE_WARNING_DAYS:
        return [
            f"WARNING: tls/cert.pem expires on {certificate['notAfter']}, in {days} days: renew it"
        ]
    return [f"tls/cert.pem names {', '.join(hosts)}, valid until {certificate['notAfter']}"]


def check_tunnel_files(directory: Path) -> list[str]:
    """The connector's token and the CA pool it checks Caddy's certificate against."""
    token = directory / "tunnel" / "token"
    if token.is_symlink() or not token.is_file():
        raise Refused("tunnel/token is missing or not a plain file")
    pool = read_certificate(directory / "tunnel" / "ca.pem")
    issuer = read_certificate(directory / "tls" / "cert.pem").get("issuer")
    if pool.get("subject") != issuer:
        return [
            "WARNING: the first certificate of tunnel/ca.pem is not the issuer of tls/cert.pem: "
            "the connector would refuse Caddy's certificate unless the pool holds it further down"
        ]
    return []


def email_links(app_env: dict[str, str], hosts: list[str]) -> list[str]:
    """PUBLIC_BASE_URL, the first trusted origin (config/settings/prod.py), is in every link that
    email carries: a host Caddy does not answer would break each one."""
    origins = _env_list(app_env, "DJANGO_CSRF_TRUSTED_ORIGINS")
    if not origins:
        raise Refused("DJANGO_CSRF_TRUSTED_ORIGINS in app.env is empty")
    lines = [f"the links in email use {origins[0]} (the first DJANGO_CSRF_TRUSTED_ORIGINS entry)"]
    if urlsplit(origins[0]).hostname not in hosts:
        lines.append(f"WARNING: Caddy answers {', '.join(hosts)}, not the host of {origins[0]}")
    return lines


def check_edge(
    env: dict[str, str], app_env: dict[str, str], directory: Path, now: float
) -> list[str]:
    """What Caddy, compose and Django will make of the edge settings, before any of them runs."""
    tls, proxy = edge_mode(env, "EDGE_TLS"), edge_mode(env, "EDGE_PROXY")
    hosts = edge_hosts(env)
    subnet = check_tunnel_network(env)
    check_tunnel_mode(env, tls, proxy)
    check_allowed_hosts(app_env, hosts)
    check_admin_reach(env, subnet, proxy)
    lines = [f"edge: EDGE_TLS={tls}, EDGE_PROXY={proxy}, the site answers {', '.join(hosts)}"]
    if proxy == "cloudflare":
        lines.append(f"edge: Caddy believes CF-Connecting-IP from {tunnel_address(env)} alone")
    if tls == "files":
        lines += check_certificate(directory, hosts, now)
    if proxy == "cloudflare":
        lines += check_tunnel_files(directory)
    return lines + email_links(app_env, hosts)


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


def _edge(args: argparse.Namespace) -> str:
    env = read_env(Path(args.env))
    lines = check_edge(env, read_env(Path(args.app_env)), Path(args.env).parent, time.time())
    return "\n".join(lines)


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
    "edge": _edge,
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
    edge = commands.add_parser("edge")
    edge.add_argument("--env", required=True)
    edge.add_argument("--app-env", required=True)
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
