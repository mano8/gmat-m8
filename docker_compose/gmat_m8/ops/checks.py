#!/usr/bin/env python3
"""What deploy.sh, recover.sh and restore-drill.sh read from JSON and notes (plan §9).

Standard library only: this runs on the host, before any image is trusted (Debian ships
python3). Each command prints plain text for the shell and exits non-zero on a mismatch.

  asset RELEASE NAME        the download URL of one asset of a GitHub release (JSON file)
  notes RELEASE             the release's notes
  flagged NOTES MARKER      exit 0 when a line of the notes starts with the marker
  manifest FILE --version V --repository OWNER/NAME --image-prefix PREFIX
                            validates a verified release-manifest.json; prints KEY=VALUE lines
  admin-cidrs VALUE         refuses an allowlist that is open or holds Docker's bridge ranges
  dump-version MANIFEST     the app version a backup manifest was taken with
  compare-counts MANIFEST RESTORED
                            the restored row counts against a backup manifest
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
OPEN_RANGES = ("0.0.0.0/0", "::/0")
IMAGE_NAMES = {"app": "gmat-m8", "backup": "gmat-m8-backup"}


class Refused(Exception):
    """The input is not what the caller may go on with."""


def _load(path: str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def asset_url(release: dict[str, Any], name: str) -> str:
    for asset in release.get("assets") or []:
        if asset.get("name") == name and asset.get("url"):
            return str(asset["url"])
    raise Refused(f"the release has no asset {name}")


def flagged(notes: str, marker: str) -> bool:
    """A release flags something with a line of its own (docs/release.md, step 1)."""
    wanted = marker.casefold()
    return any(line.strip(" \t*_-#").casefold().startswith(wanted) for line in notes.splitlines())


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
    return {
        "RELEASE_COMMIT": commit,
        "RELEASE_APP_DIGEST": app["digest"],
        "RELEASE_APP_REF": app["ref"],
        "RELEASE_BACKUP_DIGEST": backup["digest"],
        "RELEASE_BACKUP_REF": backup["ref"],
    }


def check_admin_cidrs(value: str) -> None:
    """Production names the networks that reach the admin, and never a bridge range."""
    entries = value.split()
    if not entries:
        raise Refused("ADMIN_ALLOWED_CIDRS is empty")
    if any(entry in OPEN_RANGES for entry in entries):
        raise Refused("ADMIN_ALLOWED_CIDRS is open to every network")
    for entry in entries:
        try:
            network = ipaddress.ip_network(entry, strict=False)
        except ValueError as exc:
            raise Refused(f"ADMIN_ALLOWED_CIDRS: {entry!r} is not a network") from exc
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


def _run(args: argparse.Namespace) -> str:
    match args.command:
        case "asset":
            return asset_url(_load(args.release), args.name)
        case "notes":
            return str(_load(args.release).get("body") or "")
        case "flagged":
            if not flagged(Path(args.notes).read_text(encoding="utf-8"), args.marker):
                raise SystemExit(1)
            return ""
        case "manifest":
            values = check_manifest(
                _load(args.file), args.version, args.repository, args.image_prefix
            )
            return "\n".join(f"{key}={value}" for key, value in values.items())
        case "admin-cidrs":
            check_admin_cidrs(args.value)
            return ""
        case "dump-version":
            return str(_load(args.manifest).get("app_version") or "")
        case _:
            differences = count_differences(_load(args.manifest), _load(args.restored))
            if differences:
                raise Refused("; ".join(differences))
            return ""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    asset = commands.add_parser("asset")
    asset.add_argument("release")
    asset.add_argument("name")
    commands.add_parser("notes").add_argument("release")
    marked = commands.add_parser("flagged")
    marked.add_argument("notes")
    marked.add_argument("marker")
    manifest = commands.add_parser("manifest")
    manifest.add_argument("file")
    manifest.add_argument("--version", required=True)
    manifest.add_argument("--repository", required=True)
    manifest.add_argument("--image-prefix", required=True)
    commands.add_parser("admin-cidrs").add_argument("value")
    commands.add_parser("dump-version").add_argument("manifest")
    counts = commands.add_parser("compare-counts")
    counts.add_argument("manifest")
    counts.add_argument("restored")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "manifest" and not VERSION.match(args.version):
        print(f"not a release version: {args.version}", file=sys.stderr)
        return 2
    try:
        output = _run(args)
    except (Refused, ValueError, OSError) as exc:
        print(exc, file=sys.stderr)
        return 2
    if output:
        print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
