#!/usr/bin/env python
"""Fail when ``uv.lock`` resolves a dependency below its security floor.

``pyproject.toml`` states the floor as a lower bound, but a lock can still pin an older
release (a stale ``uv.lock`` merged from another branch, a manual edit). The lock is what
the image installs, so it is what this script checks. Raise a floor here when an advisory
requires it (plan §8.3).
"""

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# normalized package name -> minimum version
FLOORS: dict[str, str] = {
    "django-allauth": "65.20.1",
}

_NORMALIZE = re.compile(r"[-_.]+")


def _normalize(name: str) -> str:
    return _NORMALIZE.sub("-", name).lower()


def _version_key(version: str) -> tuple[int, ...]:
    """Numeric release segments of ``version``; a pre/post/dev suffix is ignored."""
    parts: list[int] = []
    for segment in version.split("."):
        match = re.match(r"\d+", segment)
        if match is None:
            break
        parts.append(int(match.group()))
    return tuple(parts)


def check(lock_path: Path = ROOT / "uv.lock", floors: dict[str, str] = FLOORS) -> list[str]:
    """Return one message per floor that the lock violates or does not resolve."""
    with lock_path.open("rb") as handle:
        lock = tomllib.load(handle)
    resolved: dict[str, set[str]] = {}
    for package in lock.get("package", []):
        if "version" in package:
            resolved.setdefault(_normalize(package["name"]), set()).add(package["version"])
    problems: list[str] = []
    for name, floor in floors.items():
        versions = resolved.get(_normalize(name))
        if not versions:
            problems.append(f"{name}: not resolved in {lock_path.name} (floor {floor})")
            continue
        problems.extend(
            f"{name} {version} is below the security floor {floor}"
            for version in sorted(versions)
            if _version_key(version) < _version_key(floor)
        )
    return problems


def main() -> int:
    problems = check()
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        return 1
    print("check_security_floors: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
