import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SHA = "a" * 40
DIGEST_APP = "sha256:" + "1" * 64
DIGEST_BACKUP = "sha256:" + "2" * 64
RUN_URL = "https://github.com/mano8/gmat-m8/actions/runs/1"
CREATED = "2026-11-01T10:00:00Z"

CHANGELOG = """\
# Changelog

Intro text.

## [Unreleased]

### Added

- Something not released yet.

## [1.1.0] - 2026-11-01

### Added

- Feature B.

### Fixed

- Bug C.

## [1.0.0] - 2026-10-01

- First release.

[Unreleased]: https://example.org/compare/v1.1.0...HEAD
[1.1.0]: https://example.org/compare/v1.0.0...v1.1.0
[1.0.0]: https://example.org/releases/tag/v1.0.0
"""


@pytest.fixture
def tools(load_script: Callable[[str], ModuleType]) -> ModuleType:
    return load_script("release_tools")


def _report(**severities: int) -> dict[str, Any]:
    vulnerabilities = [
        {
            "VulnerabilityID": f"CVE-{severity}-{index}",
            "Severity": severity,
            "PkgName": "libfoo",
            "InstalledVersion": "1.0",
            "FixedVersion": "1.1" if index == 0 else "",
        }
        for severity, count in severities.items()
        for index in range(count)
    ]
    return {"Results": [{"Target": "debian", "Vulnerabilities": vulnerabilities}]}


def _release_dir(tmp_path: Path, *, backup: bool = True) -> Path:
    """The files the two build jobs leave behind."""
    names = {"gmat-m8": DIGEST_APP} | ({"gmat-m8-backup": DIGEST_BACKUP} if backup else {})
    for name, digest in names.items():
        (tmp_path / f"digest-{name}.txt").write_text(digest + "\n", encoding="utf-8")
        for arch in ("amd64", "arm64"):
            (tmp_path / f"sbom-{name}-{arch}.spdx.json").write_text("{}", encoding="utf-8")
            report = _report(HIGH=1, LOW=2) if arch == "arm64" else _report(MEDIUM=3)
            (tmp_path / f"trivy-{name}-{arch}-report.json").write_text(
                json.dumps(report), encoding="utf-8"
            )
    return tmp_path


def _manifest_args(directory: Path, **overrides: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "directory": directory,
        "version": "1.1.0",
        "commit": SHA,
        "repository": "mano8/gmat-m8",
        "registry": "ghcr.io/mano8",
        "images": {"app": "gmat-m8", "backup": "gmat-m8-backup"},
        "run_url": RUN_URL,
        "created_at": CREATED,
    }
    return args | overrides


# ---------------------------------------------------------------------------- notes


def test_notes_are_the_versions_section(tools: ModuleType) -> None:
    notes = tools.release_notes(CHANGELOG, "1.1.0")
    assert notes.startswith("### Added\n\n- Feature B.")
    assert notes.endswith("- Bug C.\n")
    assert "Something not released yet" not in notes
    assert "First release" not in notes


def test_last_section_drops_the_link_references(tools: ModuleType) -> None:
    assert tools.release_notes(CHANGELOG, "1.0.0") == "- First release.\n"


def test_unknown_version_has_no_notes(tools: ModuleType) -> None:
    with pytest.raises(tools.ReleaseError, match=r"no section \[2.0.0\]"):
        tools.release_notes(CHANGELOG, "2.0.0")


def test_empty_section_is_refused(tools: ModuleType) -> None:
    changelog = "## [1.2.0] - 2026-11-02\n\n## [1.1.0] - 2026-11-01\n\n- x\n"
    with pytest.raises(tools.ReleaseError, match="is empty"):
        tools.release_notes(changelog, "1.2.0")


def test_repository_changelog_has_an_unreleased_section(tools: ModuleType) -> None:
    changelog = (tools.ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert tools.release_notes(changelog, "Unreleased").strip()


# ------------------------------------------------------------------------- manifest


def test_severity_counts(tools: ModuleType) -> None:
    report = _report(CRITICAL=1, HIGH=2, LOW=3)
    report["Results"][0]["Vulnerabilities"].append({"Severity": "WEIRD"})
    report["Results"].append({"Secrets": [{"RuleID": "k", "Severity": "CRITICAL"}]})
    report["Results"].append({"Misconfigurations": [{"ID": "m", "Severity": "MEDIUM"}]})
    assert tools.severity_counts(report) == {
        "CRITICAL": 2,
        "HIGH": 2,
        "MEDIUM": 1,
        "LOW": 3,
        "UNKNOWN": 1,
    }


def test_a_clean_report_counts_zero(tools: ModuleType) -> None:
    assert sum(tools.severity_counts({}).values()) == 0
    assert sum(tools.severity_counts({"Results": None}).values()) == 0


def test_manifest_lists_both_images(tools: ModuleType, tmp_path: Path) -> None:
    ignorefile = tmp_path / ".trivyignore.yaml"
    ignorefile.write_bytes(b"# nothing\n")  # bytes: no newline translation on Windows
    files = tmp_path / "files"
    files.mkdir()
    manifest = tools.build_manifest(**_manifest_args(_release_dir(files), ignorefile=ignorefile))
    assert manifest["schema_version"] == 1
    assert manifest["version"] == "1.1.0"
    assert manifest["tag"] == "v1.1.0"
    assert manifest["commit"] == SHA
    assert manifest["run_url"] == RUN_URL
    assert manifest["created_at"] == CREATED
    assert set(manifest["images"]) == {"app", "backup"}

    app = manifest["images"]["app"]
    assert app["digest"] == DIGEST_APP
    assert app["ref"] == f"ghcr.io/mano8/gmat-m8@{DIGEST_APP}"
    assert app["tag"] == "1.1.0"
    assert app["platforms"] == ["linux/amd64", "linux/arm64"]
    assert app["sboms"] == [
        "sbom-gmat-m8-amd64.spdx.json",
        "sbom-gmat-m8-arm64.spdx.json",
    ]
    assert app["scan"]["linux/amd64"]["MEDIUM"] == 3
    assert app["scan"]["linux/arm64"]["HIGH"] == 1
    assert manifest["images"]["backup"]["ref"] == f"ghcr.io/mano8/gmat-m8-backup@{DIGEST_BACKUP}"

    assert manifest["scan_gate"] == {
        "severity": "CRITICAL,HIGH",
        "ignore_unfixed": False,
        "ignorefile": ".trivyignore.yaml",
        "ignorefile_sha256": hashlib.sha256(b"# nothing\n").hexdigest(),
    }


def test_manifest_without_an_ignorefile_says_so_by_omission(
    tools: ModuleType, tmp_path: Path
) -> None:
    files = tmp_path / "files"
    files.mkdir()
    manifest = tools.build_manifest(
        **_manifest_args(_release_dir(files), ignorefile=tmp_path / "missing.yaml")
    )
    assert "ignorefile" not in manifest["scan_gate"]
    assert manifest["scan_gate"]["ignore_unfixed"] is False


@pytest.mark.parametrize("version", ["v1.1.0", "1.1", "latest", ""])
def test_manifest_refuses_a_non_semantic_version(
    tools: ModuleType, tmp_path: Path, version: str
) -> None:
    with pytest.raises(tools.ReleaseError, match="semantic version"):
        tools.build_manifest(**_manifest_args(_release_dir(tmp_path), version=version))


@pytest.mark.parametrize("commit", ["main", "abc", "A" * 40])
def test_manifest_refuses_a_bad_commit(tools: ModuleType, tmp_path: Path, commit: str) -> None:
    with pytest.raises(tools.ReleaseError, match="commit SHA"):
        tools.build_manifest(**_manifest_args(_release_dir(tmp_path), commit=commit))


def test_manifest_needs_images(tools: ModuleType, tmp_path: Path) -> None:
    with pytest.raises(tools.ReleaseError, match="no images"):
        tools.build_manifest(**_manifest_args(_release_dir(tmp_path), images={}))


def test_manifest_refuses_a_missing_image(tools: ModuleType, tmp_path: Path) -> None:
    with pytest.raises(tools.ReleaseError, match=r"digest-gmat-m8-backup\.txt"):
        tools.build_manifest(**_manifest_args(_release_dir(tmp_path, backup=False)))


@pytest.mark.parametrize("digest", ["", "latest", "sha256:abc", "sha256:" + "G" * 64, "1" * 64])
def test_manifest_refuses_a_malformed_digest(
    tools: ModuleType, tmp_path: Path, digest: str
) -> None:
    directory = _release_dir(tmp_path)
    (directory / "digest-gmat-m8.txt").write_text(digest, encoding="utf-8")
    with pytest.raises(tools.ReleaseError, match="sha256 digest"):
        tools.build_manifest(**_manifest_args(directory))


def test_manifest_refuses_an_image_without_an_sbom(tools: ModuleType, tmp_path: Path) -> None:
    directory = _release_dir(tmp_path)
    for path in directory.glob("sbom-gmat-m8-backup-*"):
        path.unlink()
    with pytest.raises(tools.ReleaseError, match="no SBOM for gmat-m8-backup"):
        tools.build_manifest(**_manifest_args(directory))


def test_manifest_refuses_an_image_without_a_scan(tools: ModuleType, tmp_path: Path) -> None:
    directory = _release_dir(tmp_path)
    for arch in ("amd64", "arm64"):
        (directory / f"trivy-gmat-m8-{arch}-report.json").unlink()
    with pytest.raises(tools.ReleaseError, match="no Trivy report for gmat-m8"):
        tools.build_manifest(**_manifest_args(directory))


def test_manifest_refuses_an_unreadable_report(tools: ModuleType, tmp_path: Path) -> None:
    directory = _release_dir(tmp_path)
    (directory / "trivy-gmat-m8-amd64-report.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(tools.ReleaseError, match=r"cannot read trivy-gmat-m8-amd64-report\.json"):
        tools.build_manifest(**_manifest_args(directory))


def test_backup_image_name_does_not_pick_up_the_app_files(
    tools: ModuleType, tmp_path: Path
) -> None:
    """`sbom-gmat-m8-*` also matches `sbom-gmat-m8-backup-*`: the app must not list them."""
    directory = _release_dir(tmp_path)
    app = tools.build_manifest(**_manifest_args(directory))["images"]["app"]
    assert all("backup" not in name for name in app["sboms"])
    assert sorted(app["scan"]) == ["linux/amd64", "linux/arm64"]


# ------------------------------------------------------------------ nightly targets

CADDY_REF = "caddy:2.11.7-alpine@sha256:" + "c" * 64
POSTGRES_REF = "postgres:18-alpine@sha256:" + "d" * 64
COMPOSE = (
    "x-app: &app\n"
    "  image: ${APP_IMAGE:-gmat-m8:local}\n"
    "services:\n"
    f"  caddy:\n    image: {CADDY_REF}\n"
    f"  db:\n    image: {POSTGRES_REF}\n"
    "  backup:\n    image: ${BACKUP_IMAGE:-gmat-m8-backup:local}\n"
)


def test_targets_without_a_release(tools: ModuleType) -> None:
    assert tools.scan_targets(COMPOSE, None) == [
        {"name": "caddy", "ref": CADDY_REF},
        {"name": "postgres", "ref": POSTGRES_REF},
    ]


def test_targets_include_the_released_images(tools: ModuleType) -> None:
    manifest = {
        "images": {
            "backup": {"ref": f"ghcr.io/mano8/gmat-m8-backup@{DIGEST_BACKUP}"},
            "app": {"ref": f"ghcr.io/mano8/gmat-m8@{DIGEST_APP}"},
        }
    }
    targets = tools.scan_targets(COMPOSE, manifest)
    assert [t["name"] for t in targets] == ["caddy", "postgres", "released-app", "released-backup"]
    assert targets[2]["ref"] == f"ghcr.io/mano8/gmat-m8@{DIGEST_APP}"


def test_repository_compose_file_yields_caddy_and_postgres(tools: ModuleType) -> None:
    compose = (tools.ROOT / "docker_compose/gmat_m8/docker-compose.yml").read_text(encoding="utf-8")
    assert [t["name"] for t in tools.scan_targets(compose, None)] == ["caddy", "postgres"]


# --------------------------------------------------------------------- nightly issue


def test_no_findings_no_issue_body(tools: ModuleType) -> None:
    assert tools.scan_issue_body({"caddy": {}, "postgres": {"Results": []}}, RUN_URL) == ""


def test_issue_body_lists_findings_worst_first(tools: ModuleType) -> None:
    body = tools.scan_issue_body(
        {"caddy": _report(HIGH=1), "released-app": _report(CRITICAL=1)}, RUN_URL
    )
    assert f"[run]({RUN_URL})" in body
    assert "2 HIGH or CRITICAL" in body
    critical = body.index("| released-app | CRITICAL |")
    high = body.index("| caddy | HIGH |")
    assert critical < high
    assert "| 1.1 |" in body  # a fix is available
    assert "Per image: caddy 1, released-app 1" in body


def test_issue_body_marks_unfixed_findings(tools: ModuleType) -> None:
    body = tools.scan_issue_body({"caddy": _report(HIGH=2)}, RUN_URL)
    assert "none yet" in body


def test_issue_body_is_capped(tools: ModuleType) -> None:
    body = tools.scan_issue_body({"caddy": _report(HIGH=tools.ISSUE_ROW_LIMIT + 5)}, RUN_URL)
    assert body.count("| caddy |") == tools.ISSUE_ROW_LIMIT
    assert "5 more in the workflow artifacts" in body


# ----------------------------------------------------------------------------- CLI


def test_cli_notes(tools: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(CHANGELOG, encoding="utf-8")
    output = tmp_path / "notes.md"
    argv = ["notes", "--version", "1.1.0", "--changelog", str(changelog), "--output", str(output)]
    assert tools.main(argv) == 0
    assert output.read_text(encoding="utf-8").startswith("### Added")
    assert tools.main(["notes", "--version", "9.9.9", "--changelog", str(changelog)]) == 1
    assert "no section [9.9.9]" in capsys.readouterr().err


def test_cli_manifest_writes_sorted_json(tools: ModuleType, tmp_path: Path) -> None:
    files = tmp_path / "files"
    files.mkdir()
    directory = _release_dir(files)
    output = tmp_path / "release-manifest.json"
    argv = [
        "manifest",
        "--dir",
        str(directory),
        "--version",
        "1.1.0",
        "--commit",
        SHA,
        "--repository",
        "mano8/gmat-m8",
        "--registry",
        "ghcr.io/mano8",
        "--image",
        "app=gmat-m8",
        "--image",
        "backup=gmat-m8-backup",
        "--run-url",
        RUN_URL,
        "--created-at",
        CREATED,
        "--output",
        str(output),
    ]
    assert tools.main(argv) == 0
    text = output.read_text(encoding="utf-8")
    manifest = json.loads(text)
    assert manifest["images"]["app"]["digest"] == DIGEST_APP
    assert manifest["scan_gate"]["ignorefile"] == ".trivyignore.yaml"  # the repository's own
    assert text == json.dumps(manifest, indent=2, sort_keys=True) + "\n"


def test_cli_manifest_defaults_the_timestamp(tools: ModuleType, tmp_path: Path) -> None:
    files = tmp_path / "files"
    files.mkdir()
    directory = _release_dir(files)
    output = tmp_path / "m.json"
    argv = [
        "manifest", "--dir", str(directory), "--version", "1.1.0", "--commit", SHA,
        "--repository", "mano8/gmat-m8", "--registry", "ghcr.io/mano8",
        "--image", "app=gmat-m8", "--run-url", RUN_URL, "--output", str(output),
    ]  # fmt: skip
    assert tools.main(argv) == 0
    assert json.loads(output.read_text(encoding="utf-8"))["created_at"].endswith("Z")


@pytest.mark.parametrize("image", ["app", "=gmat-m8", "app="])
def test_cli_manifest_rejects_a_malformed_image(
    tools: ModuleType, tmp_path: Path, image: str, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = [
        "manifest", "--dir", str(tmp_path), "--version", "1.1.0", "--commit", SHA,
        "--repository", "mano8/gmat-m8", "--registry", "ghcr.io/mano8",
        "--image", image, "--run-url", RUN_URL,
    ]  # fmt: skip
    assert tools.main(argv) == 1
    assert "ROLE=NAME" in capsys.readouterr().err


def test_cli_scan_targets(
    tools: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(COMPOSE, encoding="utf-8")
    manifest = tmp_path / "release-manifest.json"
    manifest.write_text(
        json.dumps({"images": {"app": {"ref": f"ghcr.io/mano8/gmat-m8@{DIGEST_APP}"}}}),
        encoding="utf-8",
    )

    assert tools.main(["scan-targets", "--compose", str(compose)]) == 0
    captured = capsys.readouterr()
    assert [t["name"] for t in json.loads(captured.out)["include"]] == ["caddy", "postgres"]
    assert "no release manifest" in captured.err

    assert tools.main(["scan-targets", "--compose", str(compose), "--manifest", str(manifest)]) == 0
    captured = capsys.readouterr()
    assert [t["name"] for t in json.loads(captured.out)["include"]][-1] == "released-app"
    assert captured.err == ""


def test_cli_scan_issue(
    tools: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "gate-caddy.json").write_text(json.dumps(_report()), encoding="utf-8")
    output = tmp_path / "issue.md"
    argv = ["scan-issue", "--dir", str(reports), "--run-url", RUN_URL, "--output", str(output)]

    assert tools.main(argv) == 0
    assert output.read_text(encoding="utf-8") == ""
    assert "no findings" in capsys.readouterr().out

    (reports / "gate-postgres.json").write_text(json.dumps(_report(HIGH=1)), encoding="utf-8")
    assert tools.main(argv) == 0
    assert "| postgres | HIGH |" in output.read_text(encoding="utf-8")


def test_cli_scan_issue_needs_reports(
    tools: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["scan-issue", "--dir", str(tmp_path), "--run-url", RUN_URL]
    assert tools.main(argv) == 1
    assert "no gate-*.json reports" in capsys.readouterr().err
