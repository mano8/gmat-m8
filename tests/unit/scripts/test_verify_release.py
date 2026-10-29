import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

REPO = "mano8/gmat-m8"
UNKNOWN_SHA = "f" * 40


@pytest.fixture
def script(load_script: Callable[[str], ModuleType]) -> ModuleType:
    return load_script("verify_release")


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(  # noqa: S603
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.org", *args],  # noqa: S607
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def history(tmp_path: Path) -> dict[str, Any]:
    """main = A -> B; a side branch holds C (never merged). `origin/main` points at B."""
    _git(tmp_path, "init", "-q", "-b", "main")
    shas = {}
    for name in ("a", "b"):
        (tmp_path / "file").write_text(name, encoding="utf-8")
        _git(tmp_path, "add", "file")
        _git(tmp_path, "commit", "-q", "-m", name)
        shas[name] = _git(tmp_path, "rev-parse", "HEAD")
    _git(tmp_path, "update-ref", "refs/remotes/origin/main", shas["b"])
    _git(tmp_path, "switch", "-q", "-c", "side", shas["a"])
    (tmp_path / "file").write_text("c", encoding="utf-8")
    _git(tmp_path, "add", "file")
    _git(tmp_path, "commit", "-q", "-m", "c")
    shas["c"] = _git(tmp_path, "rev-parse", "HEAD")
    return {"root": tmp_path, **shas}


def _run(sha: str, **overrides: Any) -> dict[str, Any]:
    run = {
        "head_sha": sha,
        "head_branch": "main",
        "conclusion": "success",
        "event": "push",
    }
    return run | overrides


class FakeApi:
    """Answers workflow-run queries from ``runs`` (workflow file -> list of runs)."""

    def __init__(self, runs: dict[str, list[dict[str, Any]]]) -> None:
        self.runs = runs
        self.paths: list[str] = []

    def __call__(self, path: str) -> dict[str, Any]:
        self.paths.append(path)
        workflow = path.split("/workflows/")[1].split("/runs")[0]
        return {"workflow_runs": self.runs.get(workflow, [])}


def _green(sha: str, *names: str) -> FakeApi:
    return FakeApi({name: [_run(sha)] for name in names})


# ---------------------------------------------------------------------------- tag


@pytest.mark.parametrize("tag", ["v0.1.0", "v1.20.3", "v1.0.0-rc.1", "v2.0.0-beta"])
def test_semantic_tags_pass(script: ModuleType, tag: str) -> None:
    assert script.check_tag(tag) == []


@pytest.mark.parametrize("tag", ["1.0.0", "v1", "v1.0", "v1.0.0.0", "vfoo", "v1.0.0-", "v1.0.0+x"])
def test_other_tags_fail(script: ModuleType, tag: str) -> None:
    assert "not a semantic version" in script.check_tag(tag)[0]


# ----------------------------------------------------------------------- ancestry


def test_commit_on_main_passes(script: ModuleType, history: dict[str, Any]) -> None:
    for name in ("a", "b"):
        assert script.check_on_base(history[name], "origin/main", history["root"]) == []


def test_commit_off_main_fails(script: ModuleType, history: dict[str, Any]) -> None:
    problems = script.check_on_base(history["c"], "origin/main", history["root"])
    assert len(problems) == 1
    assert "not an ancestor of origin/main" in problems[0]


def test_unknown_commit_fails(script: ModuleType, history: dict[str, Any]) -> None:
    problems = script.check_on_base(UNKNOWN_SHA, "origin/main", history["root"])
    assert len(problems) == 1
    assert "cannot compare" in problems[0]


def test_unknown_base_ref_fails(script: ModuleType, history: dict[str, Any]) -> None:
    problems = script.check_on_base(history["a"], "origin/nowhere", history["root"])
    assert "cannot compare" in problems[0]


@pytest.mark.parametrize("sha", ["main", "abc123", "A" * 40, ""])
def test_ancestry_needs_a_full_sha(script: ModuleType, history: dict[str, Any], sha: str) -> None:
    assert "40-character" in script.check_on_base(sha, "origin/main", history["root"])[0]


def test_ancestry_reports_a_missing_git(
    script: ModuleType, history: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise FileNotFoundError("git")

    monkeypatch.setattr(script.subprocess, "run", boom)
    assert "cannot run git" in script.check_on_base(history["a"], "origin/main")[0]


# --------------------------------------------------------------------------- runs


def test_required_workflows_before_e2e_exists(script: ModuleType, tmp_path: Path) -> None:
    assert script.required_workflows(tmp_path) == ["ci.yaml", "compose-smoke.yaml"]


def test_e2e_becomes_required_once_the_file_exists(script: ModuleType, tmp_path: Path) -> None:
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "e2e.yaml").write_text("name: e2e\n", encoding="utf-8")
    assert script.required_workflows(tmp_path) == ["ci.yaml", "compose-smoke.yaml", "e2e.yaml"]


def test_repository_workflows_are_the_ones_checked(script: ModuleType) -> None:
    names = script.required_workflows()
    assert {"ci.yaml", "compose-smoke.yaml"} <= set(names)
    for name in names:
        assert (script.ROOT / ".github" / "workflows" / name).is_file()


def test_green_runs_pass(script: ModuleType) -> None:
    api = _green("a" * 40, "ci.yaml", "compose-smoke.yaml")
    assert script.check_runs(api, REPO, "a" * 40, ["ci.yaml", "compose-smoke.yaml"]) == []


def test_query_asks_for_this_sha_on_main(script: ModuleType) -> None:
    api = _green("a" * 40, "ci.yaml")
    script.check_runs(api, REPO, "a" * 40, ["ci.yaml"])
    assert api.paths == [
        f"repos/{REPO}/actions/workflows/ci.yaml/runs"
        f"?head_sha={'a' * 40}&branch=main&status=success&per_page=100"
    ]


@pytest.mark.parametrize(
    "override",
    [
        {"head_sha": "b" * 40},
        {"head_branch": "feature"},
        {"conclusion": "failure"},
        {"conclusion": None},
        {"event": "pull_request"},
    ],
    ids=["other-sha", "other-branch", "failed", "unfinished", "pull-request"],
)
def test_a_run_that_does_not_qualify_is_not_enough(
    script: ModuleType, override: dict[str, Any]
) -> None:
    sha = "a" * 40
    api = FakeApi({"ci.yaml": [_run(sha, **override)]})
    problems = script.check_runs(api, REPO, sha, ["ci.yaml"])
    assert problems == [f"ci.yaml has no successful run on main for commit {sha}"]


def test_one_qualifying_run_among_others_is_enough(script: ModuleType) -> None:
    sha = "a" * 40
    api = FakeApi({"ci.yaml": [_run(sha, conclusion="failure"), _run(sha, event="schedule")]})
    assert script.check_runs(api, REPO, sha, ["ci.yaml"]) == []


def test_each_missing_workflow_is_reported(script: ModuleType) -> None:
    sha = "a" * 40
    problems = script.check_runs(
        _green(sha, "ci.yaml"), REPO, sha, ["ci.yaml", "compose-smoke.yaml"]
    )
    assert len(problems) == 1
    assert "compose-smoke.yaml" in problems[0]


def test_an_api_error_is_a_failure(script: ModuleType) -> None:
    def broken(_path: str) -> None:
        raise script.ApiError("HTTP 404")

    problems = script.check_runs(broken, REPO, "a" * 40, ["ci.yaml"])
    assert problems == ["cannot read the runs of ci.yaml: HTTP 404"]


# ------------------------------------------------------------------------- verify


def test_a_main_commit_with_green_runs_may_be_published(
    script: ModuleType, history: dict[str, Any]
) -> None:
    api = _green(history["b"], "ci.yaml", "compose-smoke.yaml")
    problems = script.verify(
        sha=history["b"], repo=REPO, tag="v0.1.0", root=history["root"], fetch=api
    )
    assert problems == []


def test_a_commit_off_main_is_refused(script: ModuleType, history: dict[str, Any]) -> None:
    # Even with green runs on that SHA (a branch run is not a main run, but say it was).
    api = _green(history["c"], "ci.yaml", "compose-smoke.yaml")
    problems = script.verify(sha=history["c"], repo=REPO, root=history["root"], fetch=api)
    assert len(problems) == 1
    assert "not an ancestor" in problems[0]


def test_a_main_commit_without_the_required_runs_is_refused(
    script: ModuleType, history: dict[str, Any]
) -> None:
    api = _green(history["a"], "ci.yaml")  # no compose-smoke run
    problems = script.verify(sha=history["a"], repo=REPO, root=history["root"], fetch=api)
    assert len(problems) == 1
    assert "compose-smoke.yaml has no successful run" in problems[0]


def test_every_problem_is_listed(script: ModuleType, history: dict[str, Any]) -> None:
    problems = script.verify(
        sha=history["c"], repo=REPO, tag="release", root=history["root"], fetch=FakeApi({})
    )
    assert len(problems) == 4  # tag, ancestry, two missing workflows


def test_a_malformed_sha_skips_the_api(script: ModuleType, history: dict[str, Any]) -> None:
    api = FakeApi({})
    problems = script.verify(sha="HEAD", repo=REPO, root=history["root"], fetch=api)
    assert len(problems) == 1
    assert api.paths == []


# ------------------------------------------------------------------------ gh / CLI


def test_gh_api_returns_decoded_json(script: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        assert kwargs["timeout"] == script.TIMEOUT_SECONDS
        return subprocess.CompletedProcess(command, 0, stdout='{"workflow_runs": []}', stderr="")

    monkeypatch.setattr(script.subprocess, "run", fake_run)
    assert script.gh_api("repos/x/y") == {"workflow_runs": []}
    assert calls == [["gh", "api", "repos/x/y"]]


def test_gh_api_failure_raises(script: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="HTTP 403")

    monkeypatch.setattr(script.subprocess, "run", fake_run)
    with pytest.raises(script.ApiError, match="HTTP 403"):
        script.gh_api("repos/x/y")


def test_gh_api_without_a_message_still_explains(
    script: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 4, stdout="", stderr="")

    monkeypatch.setattr(script.subprocess, "run", fake_run)
    with pytest.raises(script.ApiError, match="exited with 4"):
        script.gh_api("repos/x/y")


def test_gh_api_missing_binary_raises(script: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise FileNotFoundError("gh")

    monkeypatch.setattr(script.subprocess, "run", boom)
    with pytest.raises(script.ApiError):
        script.gh_api("repos/x/y")


def test_main_exit_status(
    script: ModuleType,
    history: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    api = _green(history["b"], "ci.yaml", "compose-smoke.yaml")
    original = script.verify
    # `main` uses verify's defaults (this checkout, the real API): point them at the fixtures.
    monkeypatch.setattr(
        script, "verify", lambda **kwargs: original(**kwargs, root=history["root"], fetch=api)
    )
    assert script.main(["--sha", history["b"], "--repo", REPO, "--tag", "v0.1.0"]) == 0
    assert "ok" in capsys.readouterr().out

    assert script.main(["--sha", history["c"], "--repo", REPO]) == 1
    err = capsys.readouterr().err
    assert "not an ancestor" in err
    assert "do not publish" in err
