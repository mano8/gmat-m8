#!/usr/bin/env python
"""Prune the offsite restic repository, from the maintainer's machine only (plan §9, F61, F72).

The VPS's credentials are append-only, so anyone on the VPS can add snapshots but delete none.
What they could still do is poison the retention: forged snapshots timed to push real ones out
of the policy at the next prune. Hence, before any prune, this script lists the snapshots and
**refuses to run** if one has an unexpected host or tag, a timestamp in the future, or an
abnormal count per day. Such snapshots are investigated, never pruned around.

restic measures ``--keep-within`` from the newest snapshot, not from today, so a single forged
future timestamp would put every real snapshot outside the window. The refusal is what closes
that; as a second line, restic's plan is checked against the window measured from the local
clock, and the prune names exactly the snapshots that were checked, so one appended in between
is never touched.

Needs ``restic`` on PATH and the delete-capable credentials in the environment
(``RESTIC_REPOSITORY``, ``RESTIC_PASSWORD``, and ``RESTIC_REST_*`` or ``AWS_*``), which never
touch the VPS. Standard library only. Exit codes: 0 done, 1 restic failed, 2 refused.

    python scripts/offsite_prune.py --dry-run
    python scripts/offsite_prune.py
"""

import argparse
import datetime
import json
import re
import subprocess
import sys
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

EXPECTED_HOST = "gmat-m8"
EXPECTED_TAG = "gmat-m8"
PROTECTED_DAYS = 90
KEEP_WEEKLY = 4
KEEP_MONTHLY = 12
# Four scheduled backups a day (docker/backup/crontab) leave room for some pre-deploy dumps.
MAX_PER_DAY = 12
CLOCK_TOLERANCE = datetime.timedelta(minutes=10)
RESTIC_TIMEOUT_SECONDS = 3600
# The tags backup.sh writes: gmat-m8, dump:<base>, label:<label>.
ALLOWED_TAG = re.compile(
    r"^(gmat-m8|dump:gmat-[0-9TZ]+(-[A-Za-z0-9._-]{1,40})?|label:[A-Za-z0-9._-]{1,40})$"
)

type Runner = Callable[[list[str]], str]


class ResticFailed(Exception):
    """restic could not do what it was asked."""


@dataclass(frozen=True)
class Snapshot:
    id: str
    time: datetime.datetime
    host: str
    tags: tuple[str, ...]

    @property
    def short(self) -> str:
        return self.id[:8]


def parse_time(value: str) -> datetime.datetime:
    """restic writes RFC 3339 with nanoseconds; Python reads microseconds."""
    trimmed = re.sub(r"(\.\d{6})\d+", r"\1", value)
    return datetime.datetime.fromisoformat(trimmed).astimezone(datetime.UTC)


def parse_snapshots(raw: str) -> list[Snapshot]:
    return [
        Snapshot(
            id=str(item["id"]),
            time=parse_time(str(item["time"])),
            host=str(item.get("hostname", "")),
            tags=tuple(str(tag) for tag in item.get("tags") or ()),
        )
        for item in json.loads(raw) or []
    ]


def _snapshot_problems(snapshot: Snapshot, now: datetime.datetime) -> Iterable[str]:
    if snapshot.host != EXPECTED_HOST:
        yield f"{snapshot.short}: unexpected host {snapshot.host!r}"
    if EXPECTED_TAG not in snapshot.tags:
        yield f"{snapshot.short}: no {EXPECTED_TAG!r} tag"
    for tag in snapshot.tags:
        if not ALLOWED_TAG.match(tag):
            yield f"{snapshot.short}: unexpected tag {tag!r}"
    if snapshot.time > now + CLOCK_TOLERANCE:
        yield f"{snapshot.short}: timestamp in the future ({snapshot.time.isoformat()})"


def anomalies(
    snapshots: Iterable[Snapshot], now: datetime.datetime, *, max_per_day: int = MAX_PER_DAY
) -> list[str]:
    """Everything that says someone other than the backup job wrote to the repository."""
    listed = list(snapshots)
    problems = [problem for item in listed for problem in _snapshot_problems(item, now)]
    per_day = Counter(item.time.date() for item in listed)
    problems += [
        f"{day}: {count} snapshots in one day (at most {max_per_day} expected)"
        for day, count in sorted(per_day.items())
        if count > max_per_day
    ]
    return problems


def policy_arguments() -> list[str]:
    # By host alone: every snapshot holds different paths (the dump's name has its timestamp),
    # so restic's default grouping by host and paths would keep each one forever.
    return [
        "--group-by",
        "host",
        "--host",
        EXPECTED_HOST,
        "--keep-within",
        f"{PROTECTED_DAYS}d",
        "--keep-weekly",
        str(KEEP_WEEKLY),
        "--keep-monthly",
        str(KEEP_MONTHLY),
    ]


def planned_removals(raw: str) -> list[Snapshot]:
    """The snapshots ``restic forget --dry-run --json`` would remove, over all its groups."""
    plan: list[dict[str, Any]] = json.loads(raw) or []
    return parse_snapshots(
        json.dumps([item for group in plan for item in group.get("remove") or []])
    )


def inside_the_window(removals: Iterable[Snapshot], now: datetime.datetime) -> list[Snapshot]:
    start = now - datetime.timedelta(days=PROTECTED_DAYS)
    return [item for item in removals if item.time >= start]


def run_restic(arguments: list[str]) -> str:
    command = ["restic", *arguments]
    result = subprocess.run(  # noqa: S603 (fixed program, arguments from this file)
        command, capture_output=True, text=True, timeout=RESTIC_TIMEOUT_SECONDS, check=False
    )
    if result.returncode != 0:
        raise ResticFailed(f"restic {arguments[0]} failed: {result.stderr.strip()}")
    return result.stdout


def prune(
    run: Runner, now: datetime.datetime, *, dry_run: bool, max_per_day: int = MAX_PER_DAY
) -> int:
    snapshots = parse_snapshots(run(["snapshots", "--json"]))
    problems = anomalies(snapshots, now, max_per_day=max_per_day)
    if problems:
        print("REFUSED: investigate these snapshots; they are never pruned around.")
        for problem in problems:
            print(f"  {problem}")
        return 2
    removals = planned_removals(run(["forget", "--dry-run", "--json", *policy_arguments()]))
    protected = inside_the_window(removals, now)
    if protected:
        print(f"REFUSED: restic's plan removes {len(protected)} snapshot(s) of the last 90 days:")
        for item in protected:
            print(f"  {item.short} {item.time.isoformat()}")
        return 2
    print(f"{len(snapshots)} snapshots, {len(removals)} to remove.")
    for item in removals:
        print(f"  {item.short} {item.time.isoformat()}")
    if dry_run or not removals:
        return 0
    run(["forget", "--prune", *[item.id for item in removals]])
    print("Pruned.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--dry-run", action="store_true", help="check and list, remove nothing")
    parser.add_argument("--max-per-day", type=int, default=MAX_PER_DAY)
    args = parser.parse_args(argv)
    now = datetime.datetime.now(datetime.UTC)
    try:
        return prune(run_restic, now, dry_run=args.dry_run, max_per_day=args.max_per_day)
    except (ResticFailed, subprocess.TimeoutExpired, OSError) as exc:
        print(exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
