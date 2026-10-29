#!/usr/bin/env python
"""Static guard for the banned constructs listed in CLAUDE.md.

Scans application code and templates for constructs that bypass escaping, CSRF or the
service layer, and workflows for the triggers that would hand fork code a write token.
Exit status 1 lists every violation. An allowlist entry names one file and one rule and
needs a reviewer's approval in the PR that adds it.
"""

import re
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CODE_DIRS = ("apps", "config", "templates")
CODE_SUFFIXES = {".py", ".html", ".txt", ".xml"}
YAML_SUFFIXES = {".yaml", ".yml"}
SKIP_PARTS = {"migrations", "__pycache__"}

# rule name -> pattern
CODE_RULES: dict[str, re.Pattern[str]] = {
    "safe-filter": re.compile(r"\|\s*safe\b"),
    "mark_safe": re.compile(r"\bmark_safe\b"),
    "csrf_exempt": re.compile(r"\bcsrf_exempt\b"),
    "raw-sql": re.compile(r"\.raw\s*\("),
    "extra-sql": re.compile(r"\.extra\s*\("),
}

# A trigger may appear as a key, in a list or in a flow mapping, so match the bare word on
# any non-comment line.
WORKFLOW_RULES: dict[str, re.Pattern[str]] = {
    "pull_request_target": re.compile(r"(?<![\w-])pull_request_target(?![\w-])"),
    "workflow_run": re.compile(r"(?<![\w-])workflow_run(?![\w-])"),
}
YAML_COMMENT = re.compile(r"#.*$", re.MULTILINE)

# (posix path relative to the repository root, rule) pairs that are allowed.
ALLOWLIST: frozenset[tuple[str, str]] = frozenset()


def _code_files(root: Path) -> Iterator[Path]:
    for directory in CODE_DIRS:
        base = root / directory
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if (
                path.is_file()
                and path.suffix in CODE_SUFFIXES
                and not SKIP_PARTS.intersection(path.parts)
            ):
                yield path


def _workflow_files(root: Path) -> Iterator[Path]:
    base = root / ".github" / "workflows"
    if base.is_dir():
        yield from sorted([*base.glob("*.yaml"), *base.glob("*.yml")])


def _scan(
    paths: Iterable[Path],
    rules: dict[str, re.Pattern[str]],
    root: Path,
    allowlist: frozenset[tuple[str, str]],
) -> list[str]:
    violations: list[str] = []
    for path in paths:
        rel = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        if path.suffix in YAML_SUFFIXES:
            # Blank the comment but keep the line count so reported line numbers stay right.
            text = YAML_COMMENT.sub("", text)
        for rule, pattern in rules.items():
            if (rel, rule) in allowlist:
                continue
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                violations.append(f"{rel}:{line}: banned construct '{rule}'")
    return violations


def check(root: Path = ROOT, allowlist: frozenset[tuple[str, str]] = ALLOWLIST) -> list[str]:
    """Return every violation under ``root``."""
    return [
        *_scan(_code_files(root), CODE_RULES, root, allowlist),
        *_scan(_workflow_files(root), WORKFLOW_RULES, root, allowlist),
    ]


def main() -> int:
    violations = check()
    for violation in violations:
        print(violation, file=sys.stderr)
    if violations:
        print(f"{len(violations)} violation(s)", file=sys.stderr)
        return 1
    print("check_access_rules: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
