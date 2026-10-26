#!/usr/bin/env python
"""Validate ``.trivyignore.yaml`` against the exception policy of plan §8.3.

Every entry needs an ``id``, a ``statement`` (why, which component, the compensating
control, the owner) and an ``expired_at`` date that is still in the future and at most 90
days away. Trivy itself stops honouring an expired entry, so the finding returns; this
check names the entry instead, and also rejects an open-ended or far-future exception.
"""

import datetime
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
IGNORE_FILE = ROOT / ".trivyignore.yaml"

SECTIONS = ("vulnerabilities", "misconfigurations", "secrets", "licenses")
MAX_DAYS = 90
MIN_STATEMENT_LENGTH = 40


def _entry_problems(label: str, entry: Any, today: datetime.date) -> list[str]:
    if not isinstance(entry, dict):
        return [f"{label}: entry must be a mapping"]
    problems: list[str] = []
    if not entry.get("id"):
        problems.append(f"{label}: missing 'id'")
    statement = entry.get("statement")
    if not isinstance(statement, str) or len(statement.strip()) < MIN_STATEMENT_LENGTH:
        problems.append(
            f"{label}: 'statement' must state why, the component, the compensating control"
            f" and the owner (at least {MIN_STATEMENT_LENGTH} characters)"
        )
    expiry = entry.get("expired_at")
    if isinstance(expiry, datetime.datetime):
        expiry = expiry.date()
    if not isinstance(expiry, datetime.date):
        problems.append(f"{label}: 'expired_at' must be a YYYY-MM-DD date")
    elif expiry <= today:
        problems.append(f"{label}: expired on {expiry.isoformat()}")
    elif expiry > today + datetime.timedelta(days=MAX_DAYS):
        problems.append(f"{label}: expires {expiry.isoformat()}, more than {MAX_DAYS} days away")
    return problems


def check(path: Path = IGNORE_FILE, today: datetime.date | None = None) -> list[str]:
    """Return every policy violation in the ignore file (none for an empty file)."""
    today = today or datetime.datetime.now(datetime.UTC).date()
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data is None:
        return []
    if not isinstance(data, dict):
        return [f"{path.name}: top level must be a mapping"]
    problems = [f"{path.name}: unknown section '{key}'" for key in data if key not in SECTIONS]
    for section in SECTIONS:
        entries = data.get(section) or []
        if not isinstance(entries, list):
            problems.append(f"{path.name}: '{section}' must be a list")
            continue
        for index, entry in enumerate(entries):
            label = f"{path.name}: {section}[{index}]"
            if isinstance(entry, dict) and entry.get("id"):
                label += f" ({entry['id']})"
            problems.extend(_entry_problems(label, entry, today))
    return problems


def main() -> int:
    problems = check()
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        return 1
    print("check_trivyignore: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
