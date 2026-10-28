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
