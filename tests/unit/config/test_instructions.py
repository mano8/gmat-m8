"""The map of the agent instructions (root ``CLAUDE.md``, "These instructions").

The root ``CLAUDE.md`` is read at the start of every session and every sub-agent; the detail
lives in a ``CLAUDE.md`` per directory, which a tool adds only when it opens a file there. An
agent finds those files through the root's map, so the map must name each of them, every app
must have one, and every such file an instruction or a document cites must exist.
"""

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
NAME = "CLAUDE.md"
SKIPPED = {"node_modules", "__pycache__", "build", "dist", "htmlcov", "test-results"}
CITED = re.compile(r"`((?:[\w.-]+/)+CLAUDE\.md)`")
WHY = "keep the map of the root CLAUDE.md (These instructions) true"


def _nested() -> list[Path]:
    """Every ``CLAUDE.md`` below the root, as a path relative to it."""
    found = []
    for folder, dirs, files in os.walk(ROOT):
        dirs[:] = [name for name in dirs if not name.startswith(".") and name not in SKIPPED]
        if NAME in files and Path(folder) != ROOT:
            found.append(Path(folder, NAME).relative_to(ROOT))
    return sorted(found)


def _root_map() -> str:
    text = (ROOT / NAME).read_text(encoding="utf-8")
    return text.split("## These instructions", 1)[1].split("\n## ", 1)[0]


def _mapped(path: Path, root_map: str, root: str) -> bool:
    """An app's file is mapped by the ``apps/<app>/CLAUDE.md`` row and the app's Layout line."""
    if path.parts[0] == "apps" and len(path.parts) == 3:
        return "`apps/<app>/CLAUDE.md`" in root_map and f"- `{path.parts[1]}`:" in root
    return f"`{path.as_posix()}`" in root_map


def _documents() -> list[Path]:
    """The files that may send a reader to an instructions file."""
    named = [NAME, *(path.as_posix() for path in _nested()), "README.md", "CONTRIBUTING.md"]
    named.append("docker_compose/gmat_m8/README.md")
    return [ROOT / name for name in named] + sorted((ROOT / "docs").rglob("*.md"))


def test_every_app_has_its_instructions() -> None:
    apps = sorted(path.parent for path in (ROOT / "apps").glob("*/apps.py"))
    missing = [app.relative_to(ROOT).as_posix() for app in apps if not (app / NAME).is_file()]
    assert not missing, f"apps without a CLAUDE.md ({WHY}): {', '.join(missing)}"


def test_the_root_maps_every_instructions_file() -> None:
    root = (ROOT / NAME).read_text(encoding="utf-8")
    root_map = _root_map()
    unmapped = [path.as_posix() for path in _nested() if not _mapped(path, root_map, root)]
    assert not unmapped, f"files the root's map does not name ({WHY}): {', '.join(unmapped)}"


def test_every_cited_instructions_file_exists() -> None:
    broken = [
        f"{document.relative_to(ROOT).as_posix()} cites {cited}"
        for document in _documents()
        for cited in CITED.findall(document.read_text(encoding="utf-8"))
        if not (ROOT / cited).is_file()
    ]
    assert not broken, f"citations of a missing CLAUDE.md ({WHY}): {'; '.join(broken)}"


def test_no_instructions_file_points_at_itself() -> None:
    loops = [
        path.as_posix()
        for path in _nested()
        if f"`{path.as_posix()}`" in (ROOT / path).read_text(encoding="utf-8")
    ]
    assert not loops, f"a CLAUDE.md that sends the reader to itself: {', '.join(loops)}"
