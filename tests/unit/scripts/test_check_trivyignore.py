import datetime
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

TODAY = datetime.date(2026, 10, 26)
STATEMENT = "Accepted: build-only component, no network exposure, owner Eli (maintainer)."


@pytest.fixture
def script(load_script: Callable[[str], ModuleType]) -> ModuleType:
    return load_script("check_trivyignore")


def _file(tmp_path: Path, text: str) -> Path:
    path = tmp_path / ".trivyignore.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _entry(expired_at: str = "2026-12-01", statement: str = STATEMENT) -> str:
    return (
        "vulnerabilities:\n"
        "  - id: CVE-1\n"
        f"    statement: {statement!r}\n"
        f"    expired_at: {expired_at}\n"
    )


def test_repository_file_is_valid(script: ModuleType) -> None:
    assert script.check() == []


def test_comment_only_file_is_valid(script: ModuleType, tmp_path: Path) -> None:
    assert script.check(_file(tmp_path, "# nothing accepted\n"), TODAY) == []


def test_valid_entry_passes(script: ModuleType, tmp_path: Path) -> None:
    assert script.check(_file(tmp_path, _entry()), TODAY) == []


def test_boundary_of_ninety_days_passes(script: ModuleType, tmp_path: Path) -> None:
    last = (TODAY + datetime.timedelta(days=90)).isoformat()
    assert script.check(_file(tmp_path, _entry(last)), TODAY) == []


def test_expired_entry_fails(script: ModuleType, tmp_path: Path) -> None:
    problems = script.check(_file(tmp_path, _entry("2026-10-01")), TODAY)
    assert len(problems) == 1
    assert "expired on 2026-10-01" in problems[0]
    assert "CVE-1" in problems[0]


def test_entry_expiring_today_fails(script: ModuleType, tmp_path: Path) -> None:
    assert script.check(_file(tmp_path, _entry(TODAY.isoformat())), TODAY)


def test_expiry_too_far_away_fails(script: ModuleType, tmp_path: Path) -> None:
    far = (TODAY + datetime.timedelta(days=91)).isoformat()
    assert "more than 90 days" in script.check(_file(tmp_path, _entry(far)), TODAY)[0]


def test_missing_expiry_fails(script: ModuleType, tmp_path: Path) -> None:
    text = f"vulnerabilities:\n  - id: CVE-1\n    statement: '{STATEMENT}'\n"
    assert "expired_at" in script.check(_file(tmp_path, text), TODAY)[0]


def test_non_date_expiry_fails(script: ModuleType, tmp_path: Path) -> None:
    assert script.check(_file(tmp_path, _entry("soon")), TODAY)


def test_datetime_expiry_is_accepted(script: ModuleType, tmp_path: Path) -> None:
    assert script.check(_file(tmp_path, _entry("2026-12-01T00:00:00Z")), TODAY) == []


def test_short_statement_fails(script: ModuleType, tmp_path: Path) -> None:
    assert "statement" in script.check(_file(tmp_path, _entry(statement="ok")), TODAY)[0]


def test_missing_id_fails(script: ModuleType, tmp_path: Path) -> None:
    text = f"vulnerabilities:\n  - statement: '{STATEMENT}'\n    expired_at: 2026-12-01\n"
    assert "missing 'id'" in script.check(_file(tmp_path, text), TODAY)[0]


def test_non_mapping_entry_fails(script: ModuleType, tmp_path: Path) -> None:
    assert "mapping" in script.check(_file(tmp_path, "vulnerabilities:\n  - CVE-1\n"), TODAY)[0]


def test_unknown_section_fails(script: ModuleType, tmp_path: Path) -> None:
    assert "unknown section" in script.check(_file(tmp_path, "cves: []\n"), TODAY)[0]


def test_section_must_be_a_list(script: ModuleType, tmp_path: Path) -> None:
    assert "must be a list" in script.check(_file(tmp_path, "vulnerabilities: x\n"), TODAY)[0]


def test_top_level_must_be_a_mapping(script: ModuleType, tmp_path: Path) -> None:
    assert "top level" in script.check(_file(tmp_path, "- a\n"), TODAY)[0]


def test_default_today_is_used(script: ModuleType, tmp_path: Path) -> None:
    assert script.check(_file(tmp_path, _entry("2001-01-01"))) != []


def test_main_reports_status(script: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    assert script.main() == 0
    assert "ok" in capsys.readouterr().out


def test_main_fails_on_violation(
    script: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(script, "check", lambda: ["boom"])
    assert script.main() == 1
    assert "boom" in capsys.readouterr().err
