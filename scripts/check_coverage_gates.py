#!/usr/bin/env python
"""Enforce the 100 % coverage gate on the security-critical modules (plan §8.1).

The whole-project floor (90 %, branch coverage) is ``fail_under`` in ``pyproject.toml``.
This script reads the JSON report (``coverage json``) and requires every statement and
branch of ``apps/access/**``, ``apps/*/services.py`` and ``apps/*/selectors.py`` to be
covered. A path that does not exist yet simply matches nothing.
"""

import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

STRICT = re.compile(r"^apps/(access/.*\.py|[^/]+/(services|selectors)\.py)$")


def check(report: dict[str, Any]) -> list[str]:
    """Return one message per strict file with a missed statement or branch."""
    problems: list[str] = []
    for filename, data in sorted(report.get("files", {}).items()):
        path = filename.replace("\\", "/")
        if not STRICT.match(path):
            continue
        missing_lines = data.get("missing_lines", [])
        missing_branches = data.get("missing_branches", [])
        if missing_lines or missing_branches:
            problems.append(
                f"{path}: {len(missing_lines)} statement(s) and {len(missing_branches)}"
                f" branch(es) not covered (100 % required)"
            )
    return problems


def main(argv: list[str]) -> int:
    report_path = Path(argv[1]) if len(argv) > 1 else ROOT / "coverage.json"
    problems = check(json.loads(report_path.read_text(encoding="utf-8")))
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        return 1
    print("check_coverage_gates: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
