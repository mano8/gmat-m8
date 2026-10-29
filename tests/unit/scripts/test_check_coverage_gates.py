import json
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


@pytest.fixture
def script(load_script: Callable[[str], ModuleType]) -> ModuleType:
    return load_script("check_coverage_gates")


def _file(lines: int = 0, branches: int = 0) -> dict[str, Any]:
    return {"missing_lines": list(range(lines)), "missing_branches": [[1, 2]] * branches}


@pytest.mark.parametrize(
    "name",
    [
        "apps/access/rules.py",
        "apps/access/sub/deep.py",
        "apps/material_requests/services.py",
        "apps/material_requests/selectors.py",
        "apps\\handovers\\services.py",
    ],
)
def test_strict_file_with_a_miss_fails(script: ModuleType, name: str) -> None:
    assert len(script.check({"files": {name: _file(lines=1)}})) == 1
    assert len(script.check({"files": {name: _file(branches=1)}})) == 1


def test_strict_file_fully_covered_passes(script: ModuleType) -> None:
    assert script.check({"files": {"apps/access/rules.py": _file()}}) == []


@pytest.mark.parametrize(
    "name",
    [
        "apps/core/views.py",
        "apps/core/services_helpers.py",
        "config/env.py",
        "apps/a/b/services.py",
    ],
)
def test_other_files_are_ignored(script: ModuleType, name: str) -> None:
    assert script.check({"files": {name: _file(lines=5, branches=5)}}) == []


def test_empty_report_passes(script: ModuleType) -> None:
    assert script.check({}) == []


def test_main_reads_report(
    script: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"files": {"apps/access/a.py": _file()}}), encoding="utf-8")
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"files": {"apps/access/a.py": _file(2)}}), encoding="utf-8")
    assert script.main(["x", str(good)]) == 0
    assert script.main(["x", str(bad)]) == 1
    assert "apps/access/a.py" in capsys.readouterr().err


def test_main_defaults_to_repository_report(
    script: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "coverage.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(script, "ROOT", tmp_path)
    assert script.main(["x"]) == 0
