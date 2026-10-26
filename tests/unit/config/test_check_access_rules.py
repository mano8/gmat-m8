import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "check_access_rules.py"


@pytest.fixture(scope="module")
def script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_access_rules", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_repository_is_clean(script: ModuleType) -> None:
    assert script.check() == []


@pytest.mark.parametrize(
    ("rel", "text", "rule"),
    [
        ("apps/x/views.py", "from django.utils.safestring import mark_safe\n", "mark_safe"),
        ("apps/x/views.py", "@csrf_exempt\ndef v(r): ...\n", "csrf_exempt"),
        ("apps/x/selectors.py", "Model.objects.raw('select 1')\n", "raw-sql"),
        ("apps/x/selectors.py", "qs.extra(select={})\n", "extra-sql"),
        ("templates/a.html", "{{ value|safe }}\n", "safe-filter"),
        ("templates/a.html", "{{ value | safe }}\n", "safe-filter"),
        (".github/workflows/ci.yaml", "on:\n  pull_request_target:\n", "pull_request_target"),
        (".github/workflows/ci.yml", "on:\n  workflow_run:\n    workflows: [x]\n", "workflow_run"),
    ],
)
def test_banned_construct_is_reported(
    script: ModuleType, tmp_path: Path, rel: str, text: str, rule: str
) -> None:
    _write(tmp_path, rel, text)
    violations = script.check(tmp_path, frozenset())
    assert len(violations) == 1
    assert rel in violations[0]
    assert rule in violations[0]


def test_allowlist_exempts_one_file_and_rule(script: ModuleType, tmp_path: Path) -> None:
    _write(tmp_path, "apps/x/upload.py", "@csrf_exempt\ndef up(r): ...\n")
    allowed = frozenset({("apps/x/upload.py", "csrf_exempt")})
    assert script.check(tmp_path, allowed) == []
    _write(tmp_path, "apps/x/other.py", "@csrf_exempt\ndef up(r): ...\n")
    assert len(script.check(tmp_path, allowed)) == 1


def test_migrations_and_other_directories_are_not_scanned(
    script: ModuleType, tmp_path: Path
) -> None:
    _write(tmp_path, "apps/x/migrations/0001.py", "mark_safe\n")
    _write(tmp_path, "tests/x.py", "mark_safe\n")
    _write(tmp_path, "docs/a.md", "|safe\n")
    assert script.check(tmp_path, frozenset()) == []


def test_missing_directories_are_fine(script: ModuleType, tmp_path: Path) -> None:
    assert script.check(tmp_path, frozenset()) == []


def test_main_reports_exit_status(
    script: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(script, "check", lambda: [])
    assert script.main() == 0
    assert "ok" in capsys.readouterr().out
    monkeypatch.setattr(script, "check", lambda: ["apps/x.py:1: banned construct 'mark_safe'"])
    assert script.main() == 1
    assert "1 violation" in capsys.readouterr().err
