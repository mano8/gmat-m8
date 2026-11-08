"""Policy tests for ``.github/workflows`` (CLAUDE.md, Security; plan §8.4).

These run on every PR, so a workflow that weakens a rule fails before the maintainer's
review. The banned triggers themselves are checked by ``scripts/check_access_rules.py``.
"""

import os
import re
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.y*ml"))
SHA_PIN = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")
REQUIRED_JOBS = {"lint", "typecheck", "security", "test", "trivy-image", "secret-scan"}
COVERAGE_JOB = "coverage-upload"
COVERAGE_TOKENS = {"CODECOV_TOKEN", "CODACY_PROJECT_TOKEN"}


def _load(path: Path) -> dict[Any, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _triggers(workflow: dict[Any, Any]) -> dict[str, Any]:
    # YAML 1.1 reads the bare key `on` as the boolean True (hence the second lookup).
    triggers = workflow.get("on") or workflow.get(True)
    if isinstance(triggers, str):
        return {triggers: None}
    if isinstance(triggers, list):
        return dict.fromkeys(triggers)
    assert isinstance(triggers, dict)
    return triggers


def _uses(step: dict[str, Any], action: str) -> bool:
    return str(step.get("uses", "")).startswith(f"{action}@")


def _steps(workflow: dict[Any, Any]) -> list[dict[str, Any]]:
    return [step for job in workflow["jobs"].values() for step in job.get("steps", [])]


def test_workflows_exist() -> None:
    assert WORKFLOWS


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_actions_are_pinned_by_sha(path: Path) -> None:
    for step in _steps(_load(path)):
        uses = step.get("uses")
        if uses and not uses.startswith("./"):
            assert SHA_PIN.match(uses), f"{path.name}: {uses} is not pinned by a full commit SHA"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_no_path_filter_on_triggers(path: Path) -> None:
    for name, config in _triggers(_load(path)).items():
        if isinstance(config, dict):
            assert not {"paths", "paths-ignore"} & config.keys(), f"{path.name}: on.{name}"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_permissions_are_declared_and_read_only_by_default(path: Path) -> None:
    workflow = _load(path)
    assert workflow.get("permissions") == {"contents": "read"}


def _secrets(part: Any) -> set[str]:
    return set(re.findall(r"secrets\.(\w+)", yaml.safe_dump(part)))


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_only_the_run_token_is_used_as_a_secret(path: Path) -> None:
    """The coverage tokens are the one exception, in one job (the test below says why it may)."""
    workflow = _load(path)
    assert not _secrets({key: value for key, value in workflow.items() if key != "jobs"})
    for job_id, job in workflow["jobs"].items():
        tokens = COVERAGE_TOKENS if (path.name, job_id) == ("ci.yaml", COVERAGE_JOB) else set()
        assert _secrets(job) <= {"GITHUB_TOKEN"} | tokens, f"{path.name}: {job_id}"


def test_the_coverage_tokens_stay_away_from_the_code_and_from_the_required_checks() -> None:
    job = _load(ROOT / ".github" / "workflows" / "ci.yaml")["jobs"][COVERAGE_JOB]
    assert COVERAGE_JOB not in REQUIRED_JOBS
    assert job["needs"] == "test"  # a failed run sends nothing
    assert _secrets(job) == COVERAGE_TOKENS
    assert "env" not in job  # each token reaches only the steps that use it
    steps = job["steps"]
    checkout = next(step for step in steps if _uses(step, "actions/checkout"))
    assert checkout["with"]["persist-credentials"] is False
    # Nothing of the repository runs: the one script only asks whether a token exists.
    scripts = [step["run"] for step in steps if "run" in step]
    assert len(scripts) == 1
    assert not re.search(r"\b(uv|python|pytest|manage\.py|bash|scripts/)", scripts[0])
    assert 'echo "${!token}' not in scripts[0]
    # Without its token a service is skipped, so a fork's or Dependabot's pull request passes.
    codecov = next(step for step in steps if _uses(step, "codecov/codecov-action"))
    codacy = next(step for step in steps if _uses(step, "codacy/codacy-coverage-reporter-action"))
    assert codecov["if"] == "steps.tokens.outputs.codecov == 'true'"
    assert "steps.tokens.outputs.codacy == 'true'" in codacy["if"]
    assert codecov["with"]["files"] == codacy["with"]["coverage-reports"] == "coverage.xml"
    assert steps[-1] is codacy  # the one that fetches code at run time (X14) goes last


def test_the_report_names_files_from_the_repository_root() -> None:
    """With ``source`` a path starts inside ``apps/``: neither service could find the file."""
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    run = pyproject["tool"]["coverage"]["run"]
    assert run["source_pkgs"] == ["apps", "config"]
    assert "source" not in run
    steps = _load(ROOT / ".github" / "workflows" / "ci.yaml")["jobs"]["test"]["steps"]
    tests = next(step for step in steps if "--cov-report=xml:coverage.xml" in step.get("run", ""))
    assert str(tests["name"]).startswith("Serial tests, coverage of both passes")
    report = next(step for step in steps if step.get("name") == "Upload coverage report")
    assert "coverage.xml" in report["with"]["path"]


def test_the_tests_run_in_parallel_then_the_serial_ones_alone() -> None:
    """Issue #42: one coverage data file over two passes; the floor applies to both together."""
    steps = _load(ROOT / ".github" / "workflows" / "ci.yaml")["jobs"]["test"]["steps"]
    runs = [" ".join(step["run"].split()) for step in steps if "pytest" in step.get("run", "")]
    assert len(runs) == 2
    parallel, serial = runs
    assert "-n auto" in parallel
    assert '-m "not live and not e2e and not serial"' in parallel
    assert "--cov-fail-under=0" in parallel
    assert "-n " not in serial
    assert '-m "serial and not live and not e2e"' in serial
    assert "--cov-append" in serial
    assert "--cov-fail-under" not in serial


def test_ci_defines_the_required_checks() -> None:
    workflow = _load(ROOT / ".github" / "workflows" / "ci.yaml")
    assert workflow["jobs"].keys() >= REQUIRED_JOBS
    for job_id in REQUIRED_JOBS:
        assert workflow["jobs"][job_id].get("name", job_id) == job_id


def test_ci_runs_on_pull_requests_and_main() -> None:
    triggers = _triggers(_load(ROOT / ".github" / "workflows" / "ci.yaml"))
    assert triggers.keys() == {"push", "pull_request"}
    assert triggers["push"]["branches"] == ["main"]


def test_trivy_gate_does_not_ignore_unfixed() -> None:
    text = (ROOT / ".github" / "workflows" / "ci.yaml").read_text(encoding="utf-8")
    assert "ignore-unfixed: true" not in text


def test_dependabot_covers_the_ecosystems_in_use() -> None:
    config = _load(ROOT / ".github" / "dependabot.yml")
    ecosystems = {update["package-ecosystem"] for update in config["updates"]}
    assert {"github-actions", "uv", "docker", "docker-compose"} <= ecosystems
    docker_dirs = {u["directory"] for u in config["updates"] if u["package-ecosystem"] == "docker"}
    assert docker_dirs == {"/docker/app", "/docker/backup"}
    assert all(update["schedule"]["interval"] == "monthly" for update in config["updates"])


def _trivy(steps: list[dict[str, Any]], option: str, value: str) -> list[dict[str, Any]]:
    """The Trivy steps whose ``with`` sets ``option`` to ``value``."""
    trivy = [step for step in steps if _uses(step, "aquasecurity/trivy-action")]
    return [step for step in trivy if step["with"].get(option) == value]


def _trivy_image_steps() -> list[dict[str, Any]]:
    return list(_load(ROOT / ".github" / "workflows" / "ci.yaml")["jobs"]["trivy-image"]["steps"])


def test_trivy_image_job_builds_both_images() -> None:
    steps = _trivy_image_steps()
    builds = [step["with"] for step in steps if _uses(step, "docker/build-push-action")]
    built = {with_["file"]: with_ for with_ in builds}
    assert built.keys() == {"docker/app/Dockerfile", "docker/backup/Dockerfile"}
    assert all(with_["load"] is True and with_["push"] is False for with_ in built.values())


def test_trivy_image_job_gates_both_images() -> None:
    gates = [step["with"] for step in _trivy(_trivy_image_steps(), "exit-code", "1")]
    assert {gate["image-ref"] for gate in gates} == {"gmat-m8:ci-scan", "gmat-m8-backup:ci-scan"}
    assert all(gate["severity"] == "CRITICAL,HIGH" for gate in gates)


def test_trivy_image_reports_ignore_the_exception_file() -> None:
    """The all-severity reports ignore the exception file so they stay complete."""
    reports = _trivy(_trivy_image_steps(), "format", "json")
    assert len(reports) == 2
    assert all(report["env"]["TRIVY_IGNOREFILE"] == "" for report in reports)


def test_arm64_images_build_outside_the_required_checks() -> None:
    workflow = _load(ROOT / ".github" / "workflows" / "ci.yaml")
    assert "images-arm64" in workflow["jobs"]
    assert "images-arm64" not in REQUIRED_JOBS


def test_compose_smoke_uses_the_scope_smoke_gate_structure() -> None:
    workflow = _load(ROOT / ".github" / "workflows" / "compose-smoke.yaml")
    jobs = workflow["jobs"]
    assert jobs.keys() == {"scope", "smoke", "compose-smoke-gate"}
    assert jobs["smoke"]["needs"] == "scope"
    assert jobs["smoke"]["if"] == "needs.scope.outputs.run == 'true'"
    gate = jobs["compose-smoke-gate"]
    assert gate["name"] == "compose-smoke-gate"
    assert gate["if"] == "always()"
    assert set(gate["needs"]) == {"scope", "smoke"}
    # The decision is taken inside the workflow: every trigger runs it, none filters paths.
    triggers = _triggers(workflow)
    assert {"push", "pull_request", "schedule", "workflow_dispatch"} <= triggers.keys()


def test_compose_smoke_runs_the_live_tests_against_a_fresh_stack() -> None:
    workflow = _load(ROOT / ".github" / "workflows" / "compose-smoke.yaml")
    commands = "\n".join(step.get("run", "") for step in workflow["jobs"]["smoke"]["steps"])
    assert "bash init.sh --ci" in commands
    assert "docker compose up -d --build --wait" in commands
    assert "pytest -m live" in commands
    assert "docker compose down" in commands
    teardown = [s for s in workflow["jobs"]["smoke"]["steps"] if s.get("name") == "Tear down"]
    assert teardown
    assert teardown[0]["if"] == "always()"
    env = next(
        s["env"]
        for s in workflow["jobs"]["smoke"]["steps"]
        if s.get("name") == "Smoke tests through Caddy"
    )
    assert env["LIVE_REQUIRED"] == "1"  # an unreachable stack fails the run instead of skipping


def test_e2e_runs_on_main_nightly_by_hand_and_on_a_labelled_pull_request() -> None:
    workflow = _load(ROOT / ".github" / "workflows" / "e2e.yaml")
    triggers = _triggers(workflow)
    assert triggers.keys() == {"push", "pull_request", "schedule", "workflow_dispatch"}
    assert triggers["push"]["branches"] == ["main"]
    assert list(workflow["jobs"]) == ["e2e"]  # not one of the required checks (plan §8.4)
    job = workflow["jobs"]["e2e"]
    # A pull request is tested only with the label; any other trigger always runs.
    assert job["if"] == (
        "github.event_name != 'pull_request' || "
        "contains(github.event.pull_request.labels.*.name, 'e2e')"
    )
    commands = "\n".join(step.get("run", "") for step in job["steps"])
    assert "bash init.sh --ci" in commands
    assert "docker compose up -d --build --wait" in commands
    assert "pytest tests/e2e -m e2e" in commands
    assert "playwright install --with-deps chromium" in commands
    teardown = [s for s in job["steps"] if s.get("name") == "Tear down"]
    assert teardown
    assert teardown[0]["if"] == "always()"
    env = next(s["env"] for s in job["steps"] if s.get("name") == "Browser tests")
    assert env["E2E_REQUIRED"] == "1"  # an unreachable stack fails the run instead of skipping


# ------------------------------------------------------------------ release pipeline

PUBLISH = ROOT / ".github" / "workflows" / "docker-publish.yaml"
IMAGE_SCAN = ROOT / ".github" / "workflows" / "image-scan.yaml"
ONLY_ON_PUSH = "needs.verify.outputs.push == 'true'"


def _write_permissions(workflow: dict[Any, Any]) -> dict[str, set[str]]:
    return {
        job_id: {scope for scope, level in job.get("permissions", {}).items() if level == "write"}
        for job_id, job in workflow["jobs"].items()
    }


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_no_trivy_gate_ignores_unfixed(path: Path) -> None:
    workflow = _load(path)
    for step in _steps(workflow):
        assert "ignore-unfixed" not in step.get("with", {}), f"{path.name}: {step.get('name')}"
        assert "TRIVY_IGNORE_UNFIXED" not in step.get("env", {})
        assert "--ignore-unfixed" not in step.get("run", "")
    assert "TRIVY_IGNORE_UNFIXED" not in workflow.get("env", {})


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_only_the_release_workflows_hold_write_permissions(path: Path) -> None:
    granted = _write_permissions(_load(path))
    if path.name == "docker-publish.yaml":
        assert granted == {
            "verify": set(),
            "scan": set(),
            "publish": {"packages", "id-token", "attestations"},
            "release": {"contents", "id-token"},
        }
    elif path.name == "image-scan.yaml":
        assert granted == {"targets": set(), "scan": set(), "report": {"issues"}}
    else:
        assert all(not scopes for scopes in granted.values()), f"{path.name}: {granted}"


def test_publish_runs_on_version_tags_and_manual_dispatch_only() -> None:
    triggers = _triggers(_load(PUBLISH))
    assert triggers.keys() == {"push", "workflow_dispatch"}
    assert triggers["push"] == {"tags": ["v*"]}  # no branch trigger: a tag push is the release


def test_publish_verifies_first_and_pushes_only_on_a_tag_push() -> None:
    jobs = _load(PUBLISH)["jobs"]
    assert jobs.keys() == {"verify", "scan", "publish", "release"}
    assert "if" not in jobs["verify"]
    assert jobs["scan"]["needs"] == "verify"
    assert "if" not in jobs["scan"]  # a dispatch builds and scans
    assert set(jobs["publish"]["needs"]) == {"verify", "scan"}
    assert jobs["publish"]["if"] == ONLY_ON_PUSH
    assert set(jobs["release"]["needs"]) == {"verify", "publish"}
    assert jobs["release"]["if"] == ONLY_ON_PUSH


def test_publish_verification_checks_main_and_the_required_runs() -> None:
    steps = _load(PUBLISH)["jobs"]["verify"]["steps"]
    checkout = next(step for step in steps if _uses(step, "actions/checkout"))
    assert checkout["with"]["fetch-depth"] == 0  # ancestry needs the history of main
    commands = "\n".join(step.get("run", "") for step in steps)
    assert "git fetch --no-tags origin main:refs/remotes/origin/main" in commands
    assert 'python scripts/verify_release.py "${args[@]}"' in commands
    assert '--tag "$REF_NAME"' in commands  # the tag is checked on a push, not on a dispatch
    assert "release_tools.py notes" in commands  # no notes, no release, and it fails early


def test_scan_covers_both_images_for_both_architectures() -> None:
    job = _load(PUBLISH)["jobs"]["scan"]
    assert job["permissions"] == {"contents": "read"}  # nothing to push, sign or release here
    legs = {(entry["key"], entry["arch"]): entry for entry in job["strategy"]["matrix"]["include"]}
    assert legs.keys() == {
        ("app", "amd64"),
        ("app", "arm64"),
        ("backup", "amd64"),
        ("backup", "arm64"),
    }
    assert {entry["dockerfile"] for entry in legs.values()} == {
        "docker/app/Dockerfile",
        "docker/backup/Dockerfile",
    }


def test_scan_builds_each_leg_without_pushing() -> None:
    steps = _load(PUBLISH)["jobs"]["scan"]["steps"]
    builds = [step["with"] for step in steps if _uses(step, "docker/build-push-action")]
    assert len(builds) == 1
    assert builds[0]["platforms"] == "linux/${{ matrix.arch }}"
    assert builds[0]["push"] is False
    assert builds[0]["load"] is True


def test_scan_gates_and_reports_each_leg() -> None:
    steps = _load(PUBLISH)["jobs"]["scan"]["steps"]
    gates = [step["with"] for step in _trivy(steps, "exit-code", "1")]
    assert len(gates) == 1
    assert gates[0]["severity"] == "CRITICAL,HIGH"
    reports = _trivy(steps, "format", "json")
    assert len(reports) == 1
    assert reports[0]["env"]["TRIVY_IGNOREFILE"] == ""  # the report keeps accepted findings
    sboms = [step["with"] for step in steps if _uses(step, "anchore/sbom-action")]
    assert [sbom["format"] for sbom in sboms] == ["spdx-json"]


def test_scan_cannot_publish() -> None:
    for step in _load(PUBLISH)["jobs"]["scan"]["steps"]:
        assert not _uses(step, "docker/login-action")
        assert not _uses(step, "sigstore/cosign-installer")
        assert not _uses(step, "actions/attest-build-provenance")
        assert "cosign" not in step.get("run", "")
        assert step.get("with", {}).get("push") is not True


def test_publish_pushes_both_architectures_by_digest_without_extra_attestations() -> None:
    job = _load(PUBLISH)["jobs"]["publish"]
    keys = {entry["key"] for entry in job["strategy"]["matrix"]["include"]}
    assert keys == {"app", "backup"}
    steps = job["steps"]
    push = next(s for s in steps if _uses(s, "docker/build-push-action"))
    assert push["with"]["push"] is True
    assert push["with"]["platforms"] == "linux/amd64,linux/arm64"
    assert push["with"]["provenance"] is False
    assert push["with"]["sbom"] is False
    attest = next(s for s in steps if _uses(s, "actions/attest-build-provenance"))
    assert attest["with"]["subject-digest"] == "${{ steps.push.outputs.digest }}"
    assert attest["with"]["push-to-registry"] is True


def test_publish_gates_the_pushed_digest_before_it_signs() -> None:
    """What is signed is what was scanned, whatever the build cache did."""
    steps = _load(PUBLISH)["jobs"]["publish"]["steps"]
    pushed = next(i for i, step in enumerate(steps) if _uses(step, "docker/build-push-action"))
    signed = next(i for i, step in enumerate(steps) if "cosign sign --yes" in step.get("run", ""))
    gates = [
        (i, step)
        for i, step in enumerate(steps)
        if _uses(step, "aquasecurity/trivy-action") and step["with"].get("exit-code") == "1"
    ]
    assert [step["env"]["TRIVY_PLATFORM"] for _, step in gates] == ["linux/amd64", "linux/arm64"]
    for index, step in gates:
        assert pushed < index < signed
        assert step["with"]["image-ref"] == "${{ env.IMAGE }}@${{ steps.push.outputs.digest }}"
        assert step["with"]["severity"] == "CRITICAL,HIGH"


def test_publish_signs_and_verifies_with_the_workflow_identity() -> None:
    text = PUBLISH.read_text(encoding="utf-8")
    assert text.count('--certificate-identity "https://github.com/${GITHUB_WORKFLOW_REF}"') == 2
    assert text.count("--certificate-oidc-issuer https://token.actions.githubusercontent.com") == 2
    assert "cosign sign --yes" in text
    assert "cosign sign-blob --yes" in text
    assert "cosign verify-blob" in text


def test_publish_release_attaches_the_signed_manifest_sboms_and_reports() -> None:
    steps = _load(PUBLISH)["jobs"]["release"]["steps"]
    commands = "\n".join(step.get("run", "") for step in steps)
    assert "release_tools.py manifest" in commands
    assert "--image app=gmat-m8" in commands
    assert "--image backup=gmat-m8-backup" in commands
    assert "gh release create" in commands
    assert "--verify-tag" in commands
    for asset in (
        "release/release-manifest.json",
        "release/release-manifest.json.sigstore.json",
        "release/sbom-*.spdx.json",
        "release/trivy-*-report.json",
    ):
        assert asset in commands
    # The manifest is signed before the release exists.
    assert commands.index("cosign sign-blob") < commands.index("gh release create")


def test_publish_artifacts_feed_the_release_by_name() -> None:
    """The release job downloads `release-*`; every artifact it needs must match that."""
    jobs = _load(PUBLISH)["jobs"]
    download = next(s for s in jobs["release"]["steps"] if _uses(s, "actions/download-artifact"))
    assert download["with"]["pattern"] == "release-*"
    for job_id in ("scan", "publish"):
        uploads = [s for s in jobs[job_id]["steps"] if _uses(s, "actions/upload-artifact")]
        assert uploads
        assert all(upload["with"]["name"].startswith("release-") for upload in uploads)


def test_publish_inputs_are_not_expanded_into_scripts() -> None:
    """Tag and ref names reach shell steps through `env`, never as `${{ }}` text."""
    for step in _steps(_load(PUBLISH)):
        assert "${{" not in step.get("run", ""), f"expression inside a script: {step.get('name')}"


def test_release_workflows_call_scripts_that_exist() -> None:
    for path in (PUBLISH, IMAGE_SCAN):
        text = path.read_text(encoding="utf-8")
        scripts = set(re.findall(r"python scripts/(\w+\.py)", text))
        assert scripts, path.name
        for script in scripts:
            assert (ROOT / "scripts" / script).is_file()


def test_image_scan_runs_nightly_and_on_demand() -> None:
    assert _triggers(_load(IMAGE_SCAN)).keys() == {"schedule", "workflow_dispatch"}


def test_image_scan_gate_and_issue_flow() -> None:
    jobs = _load(IMAGE_SCAN)["jobs"]
    assert jobs.keys() == {"targets", "scan", "report"}
    assert jobs["scan"]["strategy"]["matrix"] == "${{ fromJSON(needs.targets.outputs.matrix) }}"
    assert jobs["scan"]["strategy"]["fail-fast"] is False
    trivy = [s for s in jobs["scan"]["steps"] if _uses(s, "aquasecurity/trivy-action")]
    assert len(trivy) == 2
    # Each image is an amd64 + arm64 index: both Trivy steps scan the matrix leg's platform.
    assert all(s["env"]["TRIVY_PLATFORM"] == "${{ matrix.platform }}" for s in trivy)
    gate = next(step for step in jobs["scan"]["steps"] if step.get("id") == "gate")
    assert gate["continue-on-error"] is True
    assert gate["with"]["severity"] == "CRITICAL,HIGH"
    assert gate["with"]["exit-code"] == "1"
    final = jobs["scan"]["steps"][-1]
    assert final["if"] == "steps.gate.outcome == 'failure'"  # the cell still fails
    assert jobs["report"]["if"] == "${{ !cancelled() }}"  # a scan that never ran says so too
    assert jobs["report"]["permissions"]["issues"] == "write"
    assert jobs["scan"]["permissions"] == {"contents": "read", "packages": "read"}


def test_image_scan_trusts_only_the_signed_manifest_of_a_published_release() -> None:
    targets = _load(IMAGE_SCAN)["jobs"]["targets"]
    assert any(_uses(step, "sigstore/cosign-installer") for step in targets["steps"])
    script = next(step for step in targets["steps"] if step.get("id") == "matrix")["run"]
    assert "(.draft or .prerelease) | not" in script
    verify = script.index("cosign verify-blob")
    assert verify < script.index("--manifest latest/release-manifest.json")
    assert "docker-publish.yaml@refs/tags/${tag}" in script
    assert "--certificate-oidc-issuer https://token.actions.githubusercontent.com" in script


def test_image_scan_closes_the_issue_only_after_a_complete_clean_run() -> None:
    step = next(
        s
        for s in _load(IMAGE_SCAN)["jobs"]["report"]["steps"]
        if s.get("name") == "Open, update or close the issue"
    )
    assert step["env"]["SCAN_RESULT"] == "${{ needs.scan.result }}"
    assert '[ "$SCAN_RESULT" = "success" ]' in step["run"]
    assert "gh issue close" in step["run"]
    assert step["run"].index("gh issue create") < step["run"].index("gh issue close")


def _issue_step() -> dict[str, Any]:
    steps = _load(IMAGE_SCAN)["jobs"]["report"]["steps"]
    return next(step for step in steps if step.get("name") == "Open, update or close the issue")


@pytest.mark.parametrize(
    ("scan_result", "findings", "open_issue", "expected"),
    [
        ("skipped", None, "", "issue create"),  # `targets` failed: nothing was scanned
        ("failure", "", "", "issue create"),  # a leg ended without a report
        ("failure", "", "7", "issue edit 7"),
        ("failure", "a finding", "", "issue create"),
        ("success", "a finding", "7", "issue edit 7"),
        ("success", "", "7", "issue close 7"),
        ("success", "", "", None),
    ],
)
def test_the_nightly_issue_also_says_when_the_scan_did_not_finish(
    tmp_path: Path, scan_result: str, findings: str | None, open_issue: str, expected: str | None
) -> None:
    """The step's own script, against a ``gh`` that only records what it was asked."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "gh.log"
    fake = bin_dir / "gh"
    fake.write_text(
        f'#!/bin/bash\necho "$*" >> "{log}"\n'
        f'if [ "$1 $2" = "issue list" ]; then echo "{open_issue}"; fi\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)
    if findings is not None:
        (tmp_path / "scan-issue.md").write_text(findings, encoding="utf-8")
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "ISSUE_TITLE": "the title",
        "SCAN_RESULT": scan_result,
        "RUN_URL": "https://example.invalid/run",
    }
    subprocess.run(  # noqa: S603
        ["bash", "-c", _issue_step()["run"]],  # noqa: S607
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
    )
    asked = [line for line in log.read_text(encoding="utf-8").splitlines() if "list" not in line]
    if expected is None:
        assert asked == []
        return
    assert len(asked) == 1
    assert asked[0].startswith(expected)
    if not findings and scan_result != "success":
        body = (tmp_path / "scan-issue.md").read_text(encoding="utf-8")
        assert "did not finish" in body
        assert env["RUN_URL"] in body


def test_the_test_job_proves_every_migration_reverses() -> None:
    """To zero and back passes through any earlier head, which ``recover.sh migrate-back`` needs
    (plan §9, M8.4)."""
    workflow = _load(ROOT / ".github" / "workflows" / "ci.yaml")
    runs = [step.get("run", "") for step in workflow["jobs"]["test"]["steps"]]
    assert any("scripts/check_migrations_reversible.py" in run for run in runs)
