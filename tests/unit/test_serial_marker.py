"""Tests that depend on wall-clock time carry the ``serial`` marker.

CI runs the suite under pytest-xdist (``-m "not serial"``), then the ``serial`` tests alone. A
race test whose second thread starts after a fixed head start, or a test with a time budget,
flips or misses its budget when other workers share the CPU. This finds them in the source:

- in ``tests/integration``, a module that calls ``time.sleep`` or ``time.perf_counter``;
- elsewhere, a test that calls ``time.perf_counter`` (a budget; a sleep there only waits for
  a timer or stands in for a hung process).

Such a module is marked as a whole (``pytestmark``), or each such test is decorated.
"""

from __future__ import annotations

import ast
from pathlib import Path

TESTS = Path(__file__).resolve().parents[1]
OUTSIDE = {"e2e", "live", "rehearsal"}  # never run under pytest-xdist
TIMING = {"sleep", "perf_counter"}


def timing_calls(node: ast.AST, names: set[str]) -> bool:
    return any(
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "time"
        and call.func.attr in names
        for call in ast.walk(node)
    )


def marks_serial(node: ast.AST) -> bool:
    return any(isinstance(item, ast.Attribute) and item.attr == "serial" for item in ast.walk(node))


def module_is_serial(tree: ast.Module) -> bool:
    return any(
        isinstance(stmt, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "pytestmark" for target in stmt.targets
        )
        and marks_serial(stmt.value)
        for stmt in tree.body
    )


def _test_functions(tree: ast.Module) -> list[ast.FunctionDef]:
    found = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    for cls in (node for node in tree.body if isinstance(node, ast.ClassDef)):
        found += [node for node in cls.body if isinstance(node, ast.FunctionDef)]
    return [node for node in found if node.name.startswith("test")]


def unmarked(path: Path, root: Path = TESTS) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    if module_is_serial(tree):
        return []
    name = path.relative_to(root).as_posix()
    if path.parent.name == "integration":
        return [name] if timing_calls(tree, TIMING) else []
    return [
        f"{name}::{test.name}"
        for test in _test_functions(tree)
        if timing_calls(test, {"perf_counter"})
        and not any(marks_serial(decorator) for decorator in test.decorator_list)
    ]


def test_every_timing_test_is_serial() -> None:
    found = [
        item
        for path in sorted(TESTS.rglob("test_*.py"))
        if not OUTSIDE & set(path.relative_to(TESTS).parts)
        for item in unmarked(path)
    ]
    assert found == [], "mark these `serial` (pytest.mark.serial): " + ", ".join(found)


def test_the_guard_notices_what_is_not_marked(tmp_path: Path) -> None:
    budget = "import time\n\n\ndef test_fast():\n    started = time.perf_counter()\n"
    race = "import time\n\n\ndef later():\n    time.sleep(0.15)\n"
    files = {
        "unit/test_budget.py": budget,
        "unit/test_marked.py": budget.replace(
            "def test_fast", "@pytest.mark.serial\ndef test_fast"
        ),
        "unit/test_waits.py": race,
        "integration/test_race.py": race,
        "integration/test_marked.py": race + "pytestmark = [pytest.mark.serial]\n",
    }
    for name, source in files.items():
        (tmp_path / name).parent.mkdir(exist_ok=True)
        (tmp_path / name).write_text(source)
    found = [item for name in files for item in unmarked(tmp_path / name, tmp_path)]
    assert found == ["unit/test_budget.py::test_fast", "integration/test_race.py"]
