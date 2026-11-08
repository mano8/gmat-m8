"""scripts/vendor_icons.py (ADR-0027): the Lucide subset the application draws."""

import json
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

NODES = {
    "a": [["path", {"d": "M0 0"}]],
    "b": [["circle", {"cx": "1", "cy": "1", "r": "1"}], ["line", {"x1": "0"}]],
    "c": [["script", {"src": "x"}]],
}


@pytest.fixture
def script(load_script: Callable[[str], ModuleType]) -> ModuleType:
    return load_script("vendor_icons")


def test_keeps_only_the_named_icons_sorted(script: ModuleType) -> None:
    assert list(script.subset(NODES, ["b", "a", "a"])) == ["a", "b"]


def test_an_unknown_icon_is_an_error(script: ModuleType) -> None:
    with pytest.raises(SystemExit, match="not in the Lucide set: nope"):
        script.subset(NODES, ["a", "nope"])


def test_an_unexpected_element_is_an_error(script: ModuleType) -> None:
    with pytest.raises(SystemExit, match="unexpected element <script>"):
        script.subset(NODES, ["c"])


def test_writes_the_vendored_file(
    script: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "icon-nodes.json"
    source.write_text(json.dumps(NODES), encoding="utf-8")
    target = tmp_path / "icons.json"
    monkeypatch.setattr(script, "TARGET", target)
    assert script.main(["vendor_icons.py", str(source), "b", "a"]) == 0
    assert json.loads(target.read_text(encoding="utf-8")).keys() == {"a", "b"}
    assert target.read_text(encoding="utf-8").endswith("\n")


def test_without_names_it_prints_how_to_use_it(
    script: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    assert script.main(["vendor_icons.py", "x.json"]) == 2
    assert "icon-nodes.json" in capsys.readouterr().out
