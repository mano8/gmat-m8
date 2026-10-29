#!/usr/bin/env python
"""Release artifacts for ``docker-publish.yaml`` and ``image-scan.yaml`` (plan §8.4, §9).

Standard library only, so the workflows run it without installing the project.

  notes         release notes: the CHANGELOG.md section of one version
  manifest      release-manifest.json: version, commit, both digests, scan summary, SBOM names
  scan-targets  the production image set for the nightly scan, as a GitHub matrix
  scan-issue    the body of the nightly scan issue, from the gate reports

The manifest is what the maintainer's VPS trusts (``deploy.sh`` verifies its signature, then
each digest it lists), so ``manifest`` refuses anything it cannot vouch for: a malformed
digest, an image without an SBOM or a scan report, a version that is not semantic.
"""

import argparse
import datetime
import hashlib
import json
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z][0-9A-Za-z.-]*)?$")
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
IMAGE_REF_PATTERN = re.compile(r"^\s+image:\s+(?P<ref>[^\s$]+@sha256:[0-9a-f]{64})\s*$", re.M)
CHANGELOG_SECTION = re.compile(r"^## \[(?P<name>[^\]]+)\][^\n]*\n", re.M)
LINK_DEFINITION = re.compile(r"^\[[^\]]+\]:\s+\S+\s*$")

SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN")
GATE = {"severity": "CRITICAL,HIGH", "ignore_unfixed": False}
MANIFEST_SCHEMA_VERSION = 1
ISSUE_ROW_LIMIT = 60


class ReleaseError(Exception):
    """The release inputs are not good enough to publish."""


# ---------------------------------------------------------------------------- notes


def release_notes(changelog: str, version: str) -> str:
    """Return the body of the ``## [version]`` section of a Keep a Changelog file."""
    sections = list(CHANGELOG_SECTION.finditer(changelog))
    for index, match in enumerate(sections):
        if match["name"] != version:
            continue
        end = sections[index + 1].start() if index + 1 < len(sections) else len(changelog)
        lines = changelog[match.end() : end].rstrip().splitlines()
        # Link references at the foot of the file belong to the file, not to the release.
        while lines and (not lines[-1].strip() or LINK_DEFINITION.match(lines[-1])):
            lines.pop()
        body = "\n".join(lines).strip()
        if not body:
            raise ReleaseError(f"CHANGELOG.md section [{version}] is empty")
        return body + "\n"
    raise ReleaseError(f"CHANGELOG.md has no section [{version}]")


# ------------------------------------------------------------------------- manifest


def severity_counts(report: dict[str, Any]) -> dict[str, int]:
    counts = dict.fromkeys(SEVERITIES, 0)
    for finding in _findings(report):
        severity = finding["severity"] if finding["severity"] in counts else "UNKNOWN"
        counts[severity] += 1
    return counts


def _findings(report: dict[str, Any]) -> Iterator[dict[str, str]]:
    """Yield the vulnerabilities, secrets and misconfigurations of a Trivy JSON report."""
    for result in report.get("Results") or []:
        target = result.get("Target", "")
        for item in result.get("Vulnerabilities") or []:
            yield {
                "kind": "vulnerability",
                "id": item.get("VulnerabilityID", "?"),
                "severity": item.get("Severity", "UNKNOWN"),
                "package": item.get("PkgName", "?"),
                "installed": item.get("InstalledVersion", ""),
                "fixed": item.get("FixedVersion", ""),
                "target": target,
            }
        for item in result.get("Secrets") or []:
            yield {
                "kind": "secret",
                "id": item.get("RuleID", "?"),
                "severity": item.get("Severity", "UNKNOWN"),
                "package": item.get("Title", "secret"),
                "installed": "",
                "fixed": "",
                "target": target,
            }
        for item in result.get("Misconfigurations") or []:
            yield {
                "kind": "misconfiguration",
                "id": item.get("ID", "?"),
                "severity": item.get("Severity", "UNKNOWN"),
                "package": item.get("Title", "misconfiguration"),
                "installed": "",
                "fixed": "",
                "target": target,
            }


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ReleaseError(f"cannot read {path.name}: {error}") from error


def _image_entry(directory: Path, name: str, registry: str, tag: str) -> dict[str, Any]:
    """One image of the manifest, from the files its build job left in ``directory``."""
    digest_file = directory / f"digest-{name}.txt"
    try:
        digest = digest_file.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise ReleaseError(f"cannot read {digest_file.name}: {error}") from error
    if not DIGEST_PATTERN.match(digest):
        raise ReleaseError(f"{digest_file.name} does not hold a sha256 digest: {digest!r}")

    # Exact names: `gmat-m8` must not pick up the files of `gmat-m8-backup`.
    sbom_pattern = re.compile(rf"^sbom-{re.escape(name)}-(?P<arch>[a-z0-9]+)\.spdx\.json$")
    report_pattern = re.compile(rf"^trivy-{re.escape(name)}-(?P<arch>[a-z0-9]+)-report\.json$")
    files = sorted(directory.iterdir())
    sboms = [path.name for path in files if sbom_pattern.match(path.name)]
    reports = {m["arch"]: path for path in files if (m := report_pattern.match(path.name))}
    if not sboms:
        raise ReleaseError(f"no SBOM for {name} in {directory}")
    if not reports:
        raise ReleaseError(f"no Trivy report for {name} in {directory}")

    scan = {f"linux/{arch}": severity_counts(_load_json(path)) for arch, path in reports.items()}
    repository = f"{registry}/{name}"
    return {
        "repository": repository,
        "tag": tag,
        "digest": digest,
        "ref": f"{repository}@{digest}",
        "platforms": sorted(scan),
        "sboms": sboms,
        "scan": scan,
    }


def build_manifest(
    *,
    directory: Path,
    version: str,
    commit: str,
    repository: str,
    registry: str,
    images: dict[str, str],
    run_url: str,
    created_at: str,
    ignorefile: Path | None = None,
) -> dict[str, Any]:
    """Return the release manifest; ``images`` maps a role (``app``) to an image name."""
    if not VERSION_PATTERN.match(version):
        raise ReleaseError(f"{version!r} is not a semantic version")
    if not SHA_PATTERN.match(commit):
        raise ReleaseError(f"{commit!r} is not a full commit SHA")
    if not images:
        raise ReleaseError("no images given")

    gate: dict[str, Any] = dict(GATE)
    if ignorefile is not None and ignorefile.is_file():
        gate["ignorefile"] = ignorefile.name
        gate["ignorefile_sha256"] = hashlib.sha256(ignorefile.read_bytes()).hexdigest()
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "version": version,
        "tag": f"v{version}",
        "commit": commit,
        "repository": repository,
        "run_url": run_url,
        "created_at": created_at,
        "images": {
            role: _image_entry(directory, name, registry, version)
            for role, name in sorted(images.items())
        },
        "scan_gate": gate,
    }


# ------------------------------------------------------------------- nightly scan


def scan_targets(compose_text: str, manifest: dict[str, Any] | None) -> list[dict[str, str]]:
    """The production image set: Caddy and PostgreSQL from the compose file, plus the
    application and backup images of the latest release when there is one."""
    targets: list[dict[str, str]] = []
    for match in IMAGE_REF_PATTERN.finditer(compose_text):
        ref = match["ref"]
        name = ref.split("@", 1)[0].rsplit("/", 1)[-1].split(":", 1)[0]
        targets.append({"name": name, "ref": ref})
    if manifest:
        for role, image in sorted(manifest.get("images", {}).items()):
            targets.append({"name": f"released-{role}", "ref": image["ref"]})
    return targets


def scan_issue_body(reports: dict[str, dict[str, Any]], run_url: str) -> str:
    """Markdown for the nightly issue; empty when no report has a finding."""
    rows: list[tuple[int, str, dict[str, str]]] = []
    totals: dict[str, int] = {}
    for name, report in sorted(reports.items()):
        for finding in _findings(report):
            rank = SEVERITIES.index(
                finding["severity"] if finding["severity"] in SEVERITIES else "UNKNOWN"
            )
            rows.append((rank, name, finding))
            totals[name] = totals.get(name, 0) + 1
    if not rows:
        return ""
    rows.sort(key=lambda row: (row[0], row[1], row[2]["id"], row[2]["package"]))

    lines = [
        f"The nightly scan of the production image set found {len(rows)} HIGH or CRITICAL "
        "finding(s) that neither a fix nor an entry in `.trivyignore.yaml` covers "
        f"([run]({run_url})).",
        "",
        "Fix them (new base image, rebuilt release) or accept them in a pull request the "
        "maintainer approves (statement, owner, expiry at most 90 days; plan §8.3).",
        "",
        "| Image | Severity | Id | Package | Installed | Fixed |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for _, name, finding in rows[:ISSUE_ROW_LIMIT]:
        fixed = finding["fixed"] or "none yet"
        lines.append(
            f"| {name} | {finding['severity']} | {finding['id']} | {finding['package']} | "
            f"{finding['installed'] or '-'} | {fixed} |"
        )
    if len(rows) > ISSUE_ROW_LIMIT:
        lines += ["", f"{len(rows) - ISSUE_ROW_LIMIT} more in the workflow artifacts."]
    lines += ["", "Per image: " + ", ".join(f"{name} {count}" for name, count in totals.items())]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------- CLI


def _cmd_notes(args: argparse.Namespace) -> int:
    notes = release_notes(Path(args.changelog).read_text(encoding="utf-8"), args.version)
    if args.output:
        Path(args.output).write_text(notes, encoding="utf-8", newline="\n")
    print(f"release_tools: notes for {args.version} ok ({len(notes.splitlines())} lines)")
    return 0


def _parse_images(items: list[str]) -> dict[str, str]:
    images: dict[str, str] = {}
    for item in items:
        role, separator, name = item.partition("=")
        if not (separator and role and name):
            raise ReleaseError(f"--image expects ROLE=NAME, got {item!r}")
        images[role] = name
    return images


def _cmd_manifest(args: argparse.Namespace) -> int:
    images = _parse_images(args.image)
    manifest = build_manifest(
        directory=Path(args.dir),
        version=args.version,
        commit=args.commit,
        repository=args.repository,
        registry=args.registry,
        images=images,
        run_url=args.run_url,
        created_at=args.created_at
        or datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        ignorefile=Path(args.ignorefile) if args.ignorefile else None,
    )
    Path(args.output).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    print(f"release_tools: wrote {args.output}")
    return 0


def _cmd_scan_targets(args: argparse.Namespace) -> int:
    manifest = _load_json(Path(args.manifest)) if args.manifest else None
    compose_text = Path(args.compose).read_text(encoding="utf-8")
    targets = scan_targets(compose_text, manifest)
    if not manifest:
        print(
            "release_tools: no release manifest, released images are not scanned", file=sys.stderr
        )
    print(json.dumps({"include": targets}, separators=(",", ":")))
    return 0


def _cmd_scan_issue(args: argparse.Namespace) -> int:
    reports = {
        path.stem.removeprefix("gate-"): _load_json(path)
        for path in sorted(Path(args.dir).glob("gate-*.json"))
    }
    if not reports:
        raise ReleaseError(f"no gate-*.json reports in {args.dir}")
    body = scan_issue_body(reports, args.run_url)
    Path(args.output).write_text(body, encoding="utf-8", newline="\n")
    print(f"release_tools: {'findings written' if body else 'no findings'} ({args.output})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)

    notes = commands.add_parser("notes", help="extract one version's CHANGELOG section")
    notes.add_argument("--version", required=True, help="X.Y.Z, without the leading v")
    notes.add_argument("--changelog", default=str(ROOT / "CHANGELOG.md"))
    notes.add_argument("--output", help="write the notes here")
    notes.set_defaults(handler=_cmd_notes)

    manifest = commands.add_parser("manifest", help="write release-manifest.json")
    manifest.add_argument("--dir", required=True, help="digests, SBOMs and Trivy reports")
    manifest.add_argument("--version", required=True, help="X.Y.Z, without the leading v")
    manifest.add_argument("--commit", required=True)
    manifest.add_argument("--repository", required=True, help="owner/name")
    manifest.add_argument("--registry", required=True, help="e.g. ghcr.io/owner")
    manifest.add_argument("--image", action="append", required=True, metavar="ROLE=NAME")
    manifest.add_argument("--run-url", required=True)
    manifest.add_argument("--created-at", help="UTC timestamp; default: now")
    manifest.add_argument("--ignorefile", default=str(ROOT / ".trivyignore.yaml"))
    manifest.add_argument("--output", default="release-manifest.json")
    manifest.set_defaults(handler=_cmd_manifest)

    targets = commands.add_parser("scan-targets", help="matrix of images for the nightly scan")
    targets.add_argument(
        "--compose", default=str(ROOT / "docker_compose/gmat_m8/docker-compose.yml")
    )
    targets.add_argument("--manifest", help="release-manifest.json of the latest release")
    targets.set_defaults(handler=_cmd_scan_targets)

    issue = commands.add_parser("scan-issue", help="issue body from gate-*.json reports")
    issue.add_argument("--dir", required=True)
    issue.add_argument("--run-url", required=True)
    issue.add_argument("--output", default="scan-issue.md")
    issue.set_defaults(handler=_cmd_scan_issue)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except ReleaseError as error:
        print(f"release_tools: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
