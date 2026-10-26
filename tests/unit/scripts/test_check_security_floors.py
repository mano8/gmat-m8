from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture
def script(load_script: Callable[[str], ModuleType]) -> ModuleType:
    return load_script("check_security_floors")


def _lock(tmp_path: Path, *packages: tuple[str, str]) -> Path:
    body = "".join(f'[[package]]\nname = "{n}"\nversion = "{v}"\n\n' for n, v in packages)
    path = tmp_path / "uv.lock"
    path.write_text("version = 1\n\n" + body, encoding="utf-8")
    return path


def test_repository_lock_meets_the_floors(script: ModuleType) -> None:
    assert script.check() == []


def test_version_below_floor_fails(script: ModuleType, tmp_path: Path) -> None:
    lock = _lock(tmp_path, ("django-allauth", "65.20.0"))
    problems = script.check(lock, {"django-allauth": "65.20.1"})
    assert problems == ["django-allauth 65.20.0 is below the security floor 65.20.1"]


@pytest.mark.parametrize("version", ["65.20.1", "65.21.0", "66.0.0", "65.20.1.post1", "100.0"])
def test_version_at_or_above_floor_passes(script: ModuleType, tmp_path: Path, version: str) -> None:
    lock = _lock(tmp_path, ("django-allauth", version))
    assert script.check(lock, {"django-allauth": "65.20.1"}) == []


def test_numeric_not_lexicographic_comparison(script: ModuleType, tmp_path: Path) -> None:
    lock = _lock(tmp_path, ("django-allauth", "65.9.0"))
    assert script.check(lock, {"django-allauth": "65.20.1"})


def test_missing_package_fails(script: ModuleType, tmp_path: Path) -> None:
    lock = _lock(tmp_path, ("django", "6.1"))
    assert "not resolved" in script.check(lock, {"django-allauth": "65.20.1"})[0]


def test_name_normalization(script: ModuleType, tmp_path: Path) -> None:
    lock = _lock(tmp_path, ("Django_Allauth", "65.20.1"))
    assert script.check(lock, {"django-allauth": "65.20.1"}) == []


def test_unparsable_version_is_below_any_floor(script: ModuleType, tmp_path: Path) -> None:
    lock = _lock(tmp_path, ("django-allauth", "dev"))
    assert script.check(lock, {"django-allauth": "65.20.1"})


def test_package_without_version_is_skipped(script: ModuleType, tmp_path: Path) -> None:
    path = tmp_path / "uv.lock"
    path.write_text('version = 1\n\n[[package]]\nname = "gmat-m8"\n', encoding="utf-8")
    assert "not resolved" in script.check(path, {"django-allauth": "65.20.1"})[0]


def test_main_reports_status(script: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    assert script.main() == 0
    assert "ok" in capsys.readouterr().out


def test_main_fails_on_violation(
    script: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(script, "check", lambda: ["boom"])
    assert script.main() == 1
    assert "boom" in capsys.readouterr().err
