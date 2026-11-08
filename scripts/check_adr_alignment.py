"""Keep the ADRs in line with the code (ADR-0026).

Each ADR names the files it governs on its ``- **Governs:**`` line: paths or globs in
backticks, a directory ending in ``/`` for everything below it, or ``none``.

    check            every ADR has a Governs line, each entry matches a tracked file, the
                     numbers run without a gap and the index lists every ADR (``lint``).
    pr --base REF    each ADR whose governed files changed since REF is updated in the change
                     or named in the pull request's ``## ADRs`` section, read from the
                     ``PR_BODY`` environment variable (the ``adr-alignment`` check).
    which PATH...    the ADRs that govern each path.
    hook             Claude Code's ``PostToolUse`` hook: reads the tool call on stdin and, when
                     the edited file is governed, tells the session which ADRs to check.

Standard library only, so the hook runs on the host's ``python3``.
"""

import argparse
import fnmatch
import json
import os
import re
import subprocess  # nosec B404: git, with fixed arguments
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADR_DIR = "docs/adr/"
INDEX = "docs/adr/README.md"
ADR_FILE = re.compile(r"^(\d{4})-[a-z0-9-]+\.md$")
TITLE = re.compile(r"^# ADR-\d{4}: (.+)$", re.MULTILINE)
GOVERNS = re.compile(r"^- \*\*Governs:\*\*(.*)$", re.MULTILINE)
ENTRY = re.compile(r"`([^`]+)`")
MENTION = re.compile(r"\bADR-(\d{4})\b")
COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
SECTION = re.compile(r"^##\s+ADRs\s*$(.*?)(?=^##\s|\Z)", re.MULTILINE | re.DOTALL)
RULE = (
    "If the change alters, extends or contradicts the decision, update that ADR (or supersede "
    "it with a new one) in the same pull request; otherwise name it in the pull request's "
    "`## ADRs` section with why it still holds."
)


@dataclass(frozen=True)
class Adr:
    number: str
    path: str
    title: str
    governs: tuple[str, ...] | None  # None: the line is missing; () for `none`

    @property
    def name(self) -> str:
        return f"ADR-{self.number}"

    def covers(self, path: str) -> bool:
        return any(_matches(entry, path) for entry in self.governs or ())


def _matches(entry: str, path: str) -> bool:
    if entry.endswith("/"):
        return path.startswith(entry)
    return fnmatch.fnmatchcase(path, entry)


def _governs(text: str) -> tuple[str, ...] | None:
    found = GOVERNS.search(text)
    if found is None:
        return None
    return tuple(ENTRY.findall(found.group(1)))


def _adr(path: Path, root: Path) -> Adr | None:
    name = ADR_FILE.match(path.name)
    if name is None:
        return None
    text = path.read_text(encoding="utf-8")
    title = TITLE.search(text)
    return Adr(
        number=name.group(1),
        path=path.relative_to(root).as_posix(),
        title=title.group(1) if title else "",
        governs=_governs(text),
    )


def load(root: Path | None = None) -> list[Adr]:
    root = root or ROOT
    adrs = (_adr(path, root) for path in sorted((root / ADR_DIR).glob("*.md")))
    return [adr for adr in adrs if adr is not None]


def _git(root: Path, *args: str) -> list[str]:
    result = subprocess.run(  # noqa: S603  # nosec B603 B607 - fixed program, arguments from this file
        ["git", *args],  # noqa: S607
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def _record_problems(adrs: list[Adr], tracked: list[str]) -> list[str]:
    found = []
    for adr in adrs:
        if adr.governs is None:
            found.append(f"{adr.path}: no `- **Governs:**` line (paths in backticks, or none)")
        for entry in adr.governs or ():
            if not any(_matches(entry, path) for path in tracked):
                found.append(f"{adr.path}: `{entry}` matches no tracked file (moved or renamed?)")
    return found


def _numbering_problems(adrs: list[Adr]) -> list[str]:
    numbers = [int(adr.number) for adr in adrs]
    if numbers != list(range(1, len(numbers) + 1)):
        return [f"{ADR_DIR}: numbers must run 0001, 0002, … without a gap or a repeat"]
    return []


def _index_problems(adrs: list[Adr], index: str) -> list[str]:
    return [
        f"{INDEX}: {adr.name} is not listed"
        for adr in adrs
        if f"({Path(adr.path).name})" not in index
    ]


def problems(root: Path | None = None) -> list[str]:
    root = root or ROOT
    adrs = load(root)
    index = (root / INDEX).read_text(encoding="utf-8")
    return (
        _record_problems(adrs, _git(root, "ls-files"))
        + _numbering_problems(adrs)
        + _index_problems(adrs, index)
    )


def governing(adrs: Iterable[Adr], path: str) -> list[Adr]:
    """The ADRs that govern ``path``; the ADRs themselves govern nothing of their own."""
    if path.startswith(ADR_DIR):
        return []
    return [adr for adr in adrs if adr.covers(path)]


def named_in(body: str) -> set[str]:
    """The ADR numbers named in a pull request's ``## ADRs`` section (comments ignored)."""
    section = SECTION.search(COMMENT.sub("", body.replace("\r\n", "\n")))
    return set(MENTION.findall(section.group(1))) if section else set()


def unacknowledged(adrs: list[Adr], changed: list[str], body: str) -> dict[Adr, list[str]]:
    """Each ADR whose governed files changed, neither updated nor named, with those files."""
    named = named_in(body)
    missing: dict[Adr, list[str]] = {}
    for path in changed:
        for adr in governing(adrs, path):
            if adr.path not in changed and adr.number not in named:
                missing.setdefault(adr, []).append(path)
    return missing


def _report(missing: dict[Adr, list[str]]) -> str:
    lines = ["These ADRs govern files this change touches, and the change does not answer them:"]
    for adr, paths in sorted(missing.items(), key=lambda item: item[0].number):
        lines.append(f"  {adr.name} ({adr.path}): {', '.join(paths)}")
    lines.append(RULE)
    lines += ["For example:", "  ## ADRs", "  - ADR-0025: checked, the shards are unchanged."]
    return "\n".join(lines)


def hook_context(payload: dict[str, object], root: Path | None = None) -> str | None:
    """What the session is told after it edits a governed file, or None."""
    root = root or ROOT
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    raw = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        path = Path(raw).resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return None  # outside the repository
    adrs = governing(load(root), path)
    if not adrs:
        return None
    names = "; ".join(f"{adr.name} ({adr.title}, {adr.path})" for adr in adrs)
    return f"{path} is governed by {names}. Read it before going on. {RULE}"


def _cmd_check(_: argparse.Namespace) -> int:
    found = problems()
    for problem in found:
        print(problem, file=sys.stderr)
    print("check_adr_alignment: " + ("ok" if not found else f"{len(found)} problem(s)"))
    return 1 if found else 0


def _cmd_pr(args: argparse.Namespace) -> int:
    changed = _git(ROOT, "diff", "--name-only", args.base, "HEAD")
    missing = unacknowledged(load(), changed, os.environ.get("PR_BODY", ""))
    if missing:
        print(_report(missing), file=sys.stderr)
        return 1
    print(f"check_adr_alignment: ok ({len(changed)} changed file(s))")
    return 0


def _cmd_which(args: argparse.Namespace) -> int:
    adrs = load()
    for path in args.paths:
        names = ", ".join(adr.name for adr in governing(adrs, path)) or "none"
        print(f"{path}: {names}")
    return 0


def _cmd_hook(_: argparse.Namespace) -> int:
    """Never fails the edit: anything unexpected says nothing."""
    try:
        context = hook_context(json.load(sys.stdin))
    except Exception:  # a hook that cannot read its input says nothing
        return 0
    if context:
        output = {
            "hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": context}
        }
        print(json.dumps(output))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check").set_defaults(run=_cmd_check)
    pr = commands.add_parser("pr")
    pr.add_argument("--base", required=True)
    pr.set_defaults(run=_cmd_pr)
    which = commands.add_parser("which")
    which.add_argument("paths", nargs="+")
    which.set_defaults(run=_cmd_which)
    commands.add_parser("hook").set_defaults(run=_cmd_hook)
    args = parser.parse_args(argv)
    return int(args.run(args))


if __name__ == "__main__":
    sys.exit(main())
