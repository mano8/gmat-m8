"""Policy tests for ``.github/workflows`` (CLAUDE.md, Security; plan §8.4).

These run on every PR, so a workflow that weakens a rule fails before the maintainer's
review. The banned triggers themselves are checked by ``scripts/check_access_rules.py``.
"""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.y*ml"))
SHA_PIN = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")
REQUIRED_JOBS = {"lint", "typecheck", "security", "test", "trivy-image", "secret-scan"}


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


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_only_the_run_token_is_used_as_a_secret(path: Path) -> None:
    used = set(re.findall(r"secrets\.(\w+)", path.read_text(encoding="utf-8")))
    assert used <= {"GITHUB_TOKEN"}, f"{path.name} reads {sorted(used - {'GITHUB_TOKEN'})}"


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


def test_trivy_image_job_builds_and_gates_both_images() -> None:
    job = _load(ROOT / ".github" / "workflows" / "ci.yaml")["jobs"]["trivy-image"]
    steps = job["steps"]
    built = {
        step["with"]["file"]: step["with"]
        for step in steps
        if str(step.get("uses", "")).startswith("docker/build-push-action@")
    }
    assert built.keys() == {"docker/app/Dockerfile", "docker/backup/Dockerfile"}
    assert all(with_["load"] is True and with_["push"] is False for with_ in built.values())
    gates = [
        step["with"]
        for step in steps
        if str(step.get("uses", "")).startswith("aquasecurity/trivy-action@")
        and step["with"].get("exit-code") == "1"
    ]
    assert {gate["image-ref"] for gate in gates} == {"gmat-m8:ci-scan", "gmat-m8-backup:ci-scan"}
    assert all(gate["severity"] == "CRITICAL,HIGH" for gate in gates)
    # The all-severity reports ignore the exception file so they stay complete.
    reports = [
        step
        for step in steps
        if str(step.get("uses", "")).startswith("aquasecurity/trivy-action@")
        and step["with"].get("format") == "json"
    ]
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


# ------------------------------------------------------------------ release pipeline

PUBLISH = ROOT / ".github" / "workflows" / "docker-publish.yaml"
IMAGE_SCAN = ROOT / ".github" / "workflows" / "image-scan.yaml"
ONLY_ON_PUSH = "needs.verify.outputs.push == 'true'"


def _uses(step: dict[str, Any], action: str) -> bool:
    return str(step.get("uses", "")).startswith(f"{action}@")


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


def test_scan_builds_and_gates_both_images_for_both_architectures() -> None:
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
    steps = job["steps"]

    builds = [step["with"] for step in steps if _uses(step, "docker/build-push-action")]
    assert len(builds) == 1
    assert builds[0]["platforms"] == "linux/${{ matrix.arch }}"
    assert builds[0]["push"] is False
    assert builds[0]["load"] is True

    trivy = [step for step in steps if _uses(step, "aquasecurity/trivy-action")]
    gates = [step["with"] for step in trivy if step["with"].get("exit-code") == "1"]
    assert len(gates) == 1
    assert gates[0]["severity"] == "CRITICAL,HIGH"
    reports = [step for step in trivy if step["with"].get("format") == "json"]
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
    assert jobs["report"]["if"] == "always() && needs.targets.result == 'success'"
    assert jobs["report"]["permissions"]["issues"] == "write"
    assert jobs["scan"]["permissions"] == {"contents": "read", "packages": "read"}


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
