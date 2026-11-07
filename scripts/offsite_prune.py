#!/usr/bin/env python
"""Prune the offsite restic repository, from the maintainer's machine only (plan §9, F61, F72).

The VPS's credentials are append-only, so anyone on the VPS can add snapshots but delete none.
What they could still do is poison the retention: forged snapshots timed to push real ones out
of the policy at the next prune. Hence, before any prune, this script lists the snapshots and
**refuses to run** if one has an unexpected host or tag, a timestamp in the future, an abnormal
count per day, or is new since the last prune but dated before it. Such snapshots are
investigated, never pruned around.

The attack this stops: forged snapshots fill the weekly and monthly rules, so an older real one
loses its place and goes at the next prune. ``--keep-within`` protects what is inside the window
whatever was added, not what is beyond it. A forgery dated in the past looks like any other
snapshot and the VPS knows the password and the tags, so the evidence must be out of its reach:
this machine remembers what it saw at the last prune (``--state``), which a compromised VPS
cannot edit. The first run has nothing to compare with and needs ``--baseline``, after the
maintainer has looked at the list. A forgery dated after the last prune still passes: it can only
take the place of what is written after it, during a compromise that makes those suspect anyway.

As a second line, restic's plan is checked against the window measured from the local clock,
whatever restic version the maintainer has, and the prune names exactly the snapshots that were
checked, so one appended in between is never touched.

Needs ``restic`` on PATH and the delete-capable credentials in the environment
(``RESTIC_REPOSITORY``, ``RESTIC_PASSWORD``, and ``RESTIC_REST_*`` or ``AWS_*``), which never
touch the VPS. Standard library only. Exit codes: 0 done, 1 restic failed, 2 refused.

    python scripts/offsite_prune.py --dry-run
    python scripts/offsite_prune.py
    python scripts/offsite_prune.py --baseline     # the first time, or after losing the state file
"""

import argparse
import datetime
import json
import re
import subprocess  # nosec B404 - a fixed program, see run_restic
import sys
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

EXPECTED_HOST = "gmat-m8"
EXPECTED_TAG = "gmat-m8"
PROTECTED_DAYS = 90
KEEP_WEEKLY = 4
KEEP_MONTHLY = 12
# Four scheduled backups a day (docker/backup/crontab) leave room for some pre-deploy dumps.
MAX_PER_DAY = 12
CLOCK_TOLERANCE = datetime.timedelta(minutes=10)
# A backup that started before the last prune and was uploaded after it: new, and rightly dated
# a little before. Long enough for a slow upload, short enough to matter to no monthly rule.
LATE_SNAPSHOT_TOLERANCE = datetime.timedelta(days=1)
DEFAULT_STATE = Path.home() / ".config" / "gmat-m8" / "offsite-prune.json"
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


@dataclass(frozen=True)
class Seen:
    """What this machine saw at its last prune."""

    at: datetime.datetime
    ids: frozenset[str]


def load_seen(path: Path) -> Seen | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    return Seen(parse_time(str(raw["at"])), frozenset(str(item) for item in raw["ids"]))


def save_seen(path: Path, seen: Seen) -> None:
    """Replaced in one rename, mode 0600: only the maintainer's machine has it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".partial")
    partial.touch(mode=0o600)
    partial.write_text(
        json.dumps({"at": seen.at.isoformat(), "ids": sorted(seen.ids)}), encoding="utf-8"
    )
    partial.replace(path)


def back_dated(snapshots: Iterable[Snapshot], seen: Seen) -> list[str]:
    """What appeared since the last prune but claims to be older: nothing the backup job writes."""
    limit = seen.at - LATE_SNAPSHOT_TOLERANCE
    return [
        f"{item.short}: dated {item.time.isoformat()}, not there at the last prune "
        f"({seen.at.isoformat()})"
        for item in snapshots
        if item.id not in seen.ids and item.time < limit
    ]


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
    result = subprocess.run(  # noqa: S603  # nosec B603 - a fixed program, arguments from this file
        command, capture_output=True, text=True, timeout=RESTIC_TIMEOUT_SECONDS, check=False
    )
    if result.returncode != 0:
        raise ResticFailed(f"restic {arguments[0]} failed: {result.stderr.strip()}")
    return result.stdout


def _refused(title: str, lines: Iterable[str]) -> int:
    print(f"REFUSED: {title}")
    for line in lines:
        print(f"  {line}")
    return 2


def _forget(run: Runner, removals: list[Snapshot]) -> None:
    """Forget exactly what was checked. restic exits 0 when the server refused the removal
    (append-only credentials): only the listing says what really happened."""
    run(["forget", "--prune", *[item.id for item in removals]])
    left = {item.id for item in parse_snapshots(run(["snapshots", "--json"]))}
    if left & {item.id for item in removals}:
        raise ResticFailed(
            "the snapshots are still listed: are these the delete-capable credentials?"
        )
    print("Pruned.")


def _apply(run: Runner, removals: list[Snapshot], seen: Seen, state: Path) -> None:
    """Forget, then remember what is left: a prune that failed records nothing."""
    if removals:
        _forget(run, removals)
    save_seen(state, Seen(seen.at, seen.ids - {item.id for item in removals}))


def _unexplained(
    snapshots: list[Snapshot], state: Path, *, baseline: bool
) -> tuple[str, list[str]]:
    """Why these snapshots cannot be trusted against this machine's record, if so."""
    seen = load_seen(state)
    if seen is not None:
        return "these are new since the last prune but dated before it:", back_dated(
            snapshots, seen
        )
    if baseline:
        return "", []
    return (
        f"no record of an earlier prune ({state}). If every snapshot below is yours, run once "
        "with --baseline:",
        [f"{item.short} {item.time.isoformat()}" for item in snapshots] or ["(no snapshots)"],
    )


def prune(
    run: Runner,
    now: datetime.datetime,
    *,
    dry_run: bool,
    state: Path,
    baseline: bool = False,
    max_per_day: int = MAX_PER_DAY,
) -> int:
    snapshots = parse_snapshots(run(["snapshots", "--json"]))
    problems = anomalies(snapshots, now, max_per_day=max_per_day)
    if problems:
        return _refused("investigate these snapshots; they are never pruned around.", problems)
    why, listed = _unexplained(snapshots, state, baseline=baseline)
    if listed:
        return _refused(why, listed)
    removals = planned_removals(run(["forget", "--dry-run", "--json", *policy_arguments()]))
    protected = inside_the_window(removals, now)
    if protected:
        return _refused(
            f"restic's plan removes {len(protected)} snapshot(s) of the last 90 days:",
            [f"{item.short} {item.time.isoformat()}" for item in protected],
        )
    print(f"{len(snapshots)} snapshots, {len(removals)} to remove.")
    for item in removals:
        print(f"  {item.short} {item.time.isoformat()}")
    if not dry_run:
        _apply(run, removals, Seen(now, frozenset(item.id for item in snapshots)), state)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--dry-run", action="store_true", help="check and list, remove nothing")
    parser.add_argument("--max-per-day", type=int, default=MAX_PER_DAY)
    parser.add_argument(
        "--state", type=Path, default=DEFAULT_STATE, help="what the last prune saw, on this machine"
    )
    parser.add_argument(
        "--baseline",
        action="store_true",
        help="no earlier prune to compare with: trust the list as it is, once you have read it",
    )
    args = parser.parse_args(argv)
    now = datetime.datetime.now(datetime.UTC)
    try:
        return prune(
            run_restic,
            now,
            dry_run=args.dry_run,
            state=args.state,
            baseline=args.baseline,
            max_per_day=args.max_per_day,
        )
    except (ResticFailed, subprocess.TimeoutExpired, OSError) as exc:
        print(exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
