"""scripts/check_adr_alignment.py (ADR-0026): the records, the pull request check and the hook."""

import io
import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

ADR = "# ADR-{n}: Decision {n}\n\n- **Status:** Accepted\n{governs}\n## Context\n\nText.\n"


@pytest.fixture
def script(load_script: Callable[[str], ModuleType]) -> ModuleType:
    return load_script("check_adr_alignment")


def _write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")


def _git(root: Path, *args: str) -> None:
    subprocess.run(  # noqa: S603
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.org", *args],  # noqa: S607
        cwd=root,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Two ADRs, an index and the files they govern, committed."""
    _write(
        tmp_path,
        {
            "docs/adr/0001-one.md": ADR.format(n="0001", governs="- **Governs:** `apps/a.py`"),
            "docs/adr/0002-two.md": ADR.format(
                n="0002", governs="- **Governs:** `apps/b/`, `*.toml`"
            ),
            "docs/adr/README.md": "- [ADR-0001](0001-one.md)\n- [ADR-0002](0002-two.md)\n",
            "apps/a.py": "",
            "apps/b/c.py": "",
            "apps/free.py": "",
            "pyproject.toml": "",
        },
    )
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")
    return tmp_path


def test_the_repository_records_are_sound(script: ModuleType) -> None:
    assert script.problems() == []


def test_sound_records_have_no_problem(script: ModuleType, repo: Path) -> None:
    assert script.problems(repo) == []


def test_an_adr_without_a_governs_line(script: ModuleType, repo: Path) -> None:
    _write(repo, {"docs/adr/0001-one.md": ADR.format(n="0001", governs="")})
    assert script.problems(repo) == [
        "docs/adr/0001-one.md: no `- **Governs:**` line (paths in backticks, or none)"
    ]


def test_none_governs_nothing_and_is_sound(script: ModuleType, repo: Path) -> None:
    _write(repo, {"docs/adr/0001-one.md": ADR.format(n="0001", governs="- **Governs:** none")})
    assert script.problems(repo) == []
    assert script.governing(script.load(repo), "apps/a.py") == []


def test_an_entry_that_matches_no_tracked_file(script: ModuleType, repo: Path) -> None:
    _git(repo, "mv", "apps/a.py", "apps/renamed.py")
    assert script.problems(repo) == [
        "docs/adr/0001-one.md: `apps/a.py` matches no tracked file (moved or renamed?)"
    ]


def test_a_gap_in_the_numbers_and_a_missing_index_line(script: ModuleType, repo: Path) -> None:
    _write(repo, {"docs/adr/0004-four.md": ADR.format(n="0004", governs="- **Governs:** none")})
    assert script.problems(repo) == [
        "docs/adr/: numbers must run 0001, 0002, … without a gap or a repeat",
        "docs/adr/README.md: ADR-0004 is not listed",
    ]


def test_which_adrs_govern_a_path(script: ModuleType, repo: Path) -> None:
    adrs = script.load(repo)
    names = lambda path: [adr.name for adr in script.governing(adrs, path)]  # noqa: E731
    assert names("apps/a.py") == ["ADR-0001"]
    assert names("apps/b/deep/d.py") == ["ADR-0002"]  # a directory governs what is below it
    assert names("pyproject.toml") == ["ADR-0002"]
    assert names("apps/free.py") == []
    assert names("apps/bad.py") == []  # `apps/b/` is a directory, not a prefix of a name
    assert names("docs/adr/0001-one.md") == []  # an ADR governs no file of its own kind


def test_the_ads_section_ignores_comments_and_other_sections(script: ModuleType) -> None:
    body = (
        "## Summary\n\nTouches ADR-0003.\n\r\n"
        "## ADRs\n\n<!-- e.g. ADR-0004: checked -->\n- ADR-0001: updated\n- ADR-0002: checked\n\n"
        "## Tests\n\nADR-0005\n"
    )
    assert script.named_in(body) == {"0001", "0002"}
    assert script.named_in("## Summary\n\nADR-0001\n") == set()


def test_a_change_answers_each_adr_it_touches(script: ModuleType, repo: Path) -> None:
    adrs = script.load(repo)
    changed = ["apps/a.py", "apps/b/c.py", "apps/free.py"]
    missing = script.unacknowledged(adrs, changed, "")
    assert {adr.name: paths for adr, paths in missing.items()} == {
        "ADR-0001": ["apps/a.py"],
        "ADR-0002": ["apps/b/c.py"],
    }
    named = script.unacknowledged(adrs, changed, "## ADRs\n\n- ADR-0001: checked\n")
    assert [adr.name for adr in named] == ["ADR-0002"]
    updated = script.unacknowledged(adrs, [*changed, "docs/adr/0002-two.md"], "## ADRs\nADR-0001")
    assert updated == {}


def test_the_pr_command_reads_the_diff_and_the_description(
    script: ModuleType,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(script, "ROOT", repo)
    _write(repo, {"apps/a.py": "changed\n"})
    _git(repo, "commit", "-qam", "change")
    monkeypatch.setenv("PR_BODY", "## Summary\n\nNothing about decisions.\n")
    assert script.main(["pr", "--base", "HEAD^1"]) == 1
    report = capsys.readouterr().err
    assert "ADR-0001 (docs/adr/0001-one.md): apps/a.py" in report
    assert "## ADRs" in report
    monkeypatch.setenv("PR_BODY", "## ADRs\n\n- ADR-0001: checked, still holds.\n")
    assert script.main(["pr", "--base", "HEAD^1"]) == 0


def test_the_hook_names_the_adrs_of_a_governed_file(script: ModuleType, repo: Path) -> None:
    context = script.hook_context({"tool_input": {"file_path": str(repo / "apps/b/c.py")}}, repo)
    assert context.startswith("apps/b/c.py is governed by ADR-0002 (Decision 0002, ")
    assert "## ADRs" in context
    notebook = {"tool_input": {"notebook_path": str(repo / "apps/a.py")}}
    assert "ADR-0001" in script.hook_context(notebook, repo)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"tool_input": "not a mapping"},
        {"tool_input": {"file_path": ""}},
        {"tool_input": {"file_path": "/elsewhere/apps/a.py"}},
        {"tool_input": {"file_path": "apps/free.py"}},
    ],
    ids=["no-input", "bad-input", "empty-path", "outside", "ungoverned"],
)
def test_the_hook_says_nothing_otherwise(
    script: ModuleType, repo: Path, payload: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)  # a relative path resolves from the session's directory
    assert script.hook_context(payload, repo) is None


def test_the_hook_command_prints_the_hook_output(
    script: ModuleType,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(script, "ROOT", repo)
    payload = {"tool_name": "Edit", "tool_input": {"file_path": str(repo / "apps/a.py")}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert script.main(["hook"]) == 0
    output = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert output["hookEventName"] == "PostToolUse"
    assert "ADR-0001" in output["additionalContext"]
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert script.main(["hook"]) == 0  # never fails the edit
    assert capsys.readouterr().out == ""


def test_check_and_which_commands(
    script: ModuleType,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(script, "ROOT", repo)
    assert script.main(["check"]) == 0
    assert script.main(["which", "apps/a.py", "apps/free.py"]) == 0
    assert capsys.readouterr().out.splitlines()[-2:] == [
        "apps/a.py: ADR-0001",
        "apps/free.py: none",
    ]
    _write(repo, {"docs/adr/0001-one.md": ADR.format(n="0001", governs="")})
    assert script.main(["check"]) == 1
