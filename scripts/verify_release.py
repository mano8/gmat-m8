#!/usr/bin/env python
"""Release gate: what ``docker-publish.yaml`` proves before it builds anything.

A commit may be published only if
  * the tag, when there is one, is a semantic version (``vX.Y.Z`` or ``vX.Y.Z-pre.1``);
  * the commit is an ancestor of ``origin/main`` (it went through the pull-request gate);
  * ``ci.yaml`` and ``compose-smoke.yaml`` each have a successful run on that exact SHA, on
    ``main`` and not from a pull request, and so does ``e2e.yaml`` once that workflow exists
    at the commit (plan §8.4, M8.2).

Every failure is listed; exit status 1 means "do not publish". The checks need only ``git``
and a ``gh`` that can read the repository's workflow runs (``GH_TOKEN`` in Actions), so they
can be rehearsed on any commit without pushing a tag:

    python scripts/verify_release.py --repo mano8/gmat-m8 --sha <sha> [--tag vX.Y.Z]
"""

import argparse
import json
import re
import subprocess  # nosec B404 - fixed argv only, see the two calls below
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

TAG_PATTERN = re.compile(r"^v\d+\.\d+\.\d+(-[0-9A-Za-z][0-9A-Za-z.-]*)?$")
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")

REQUIRED_WORKFLOWS = ("ci.yaml", "compose-smoke.yaml")
# Required only once the file exists at the commit being released (M8.2 adds it).
CONDITIONAL_WORKFLOWS = ("e2e.yaml",)
# A run counts only when it executed on the protected branch itself: a pull-request run of
# a fork branch can be named `main` and carry the same head SHA.
BASE_BRANCH = "main"
TRUSTED_EVENTS = frozenset({"push", "schedule", "workflow_dispatch"})
TIMEOUT_SECONDS = 60

Fetch = Callable[[str], Any]


class ApiError(Exception):
    """The GitHub API call failed (network, permission or missing resource)."""


def gh_api(path: str) -> Any:
    """GET ``path`` through the GitHub CLI and return the decoded JSON."""
    try:
        result = subprocess.run(  # noqa: S603  # nosec B603 B607 - fixed argv; `path` is built from validated values
            ["gh", "api", path],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ApiError(str(error)) from error
    if result.returncode != 0:
        raise ApiError(result.stderr.strip() or f"gh api exited with {result.returncode}")
    try:
        return json.loads(result.stdout)
    except ValueError as error:
        raise ApiError(f"gh api returned something that is not JSON: {error}") from error


def check_tag(tag: str) -> list[str]:
    if TAG_PATTERN.match(tag):
        return []
    return [f"tag {tag!r} is not a semantic version tag (vX.Y.Z or vX.Y.Z-pre.N)"]


def check_on_base(sha: str, base_ref: str, root: Path = ROOT) -> list[str]:
    """The commit must be reachable from ``base_ref`` (``origin/main``)."""
    if not SHA_PATTERN.match(sha):
        return [f"{sha!r} is not a full 40-character commit SHA"]
    try:
        result = subprocess.run(  # noqa: S603  # nosec B603 B607 - fixed argv; `sha` passed the 40-hex check
            ["git", "merge-base", "--is-ancestor", sha, base_ref],  # noqa: S607
            cwd=root,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return [f"cannot run git: {error}"]
    if result.returncode == 0:
        return []
    if result.returncode == 1:
        return [f"commit {sha} is not an ancestor of {base_ref}: it never went through main"]
    return [f"cannot compare {sha} with {base_ref}: {result.stderr.strip()}"]


def required_workflows(root: Path = ROOT) -> list[str]:
    workflows = list(REQUIRED_WORKFLOWS)
    workflows += [
        name for name in CONDITIONAL_WORKFLOWS if (root / ".github" / "workflows" / name).is_file()
    ]
    return workflows


def has_successful_run(fetch: Fetch, repo: str, workflow: str, sha: str) -> bool:
    query = f"head_sha={sha}&branch={BASE_BRANCH}&status=success&per_page=100"
    data = fetch(f"repos/{repo}/actions/workflows/{workflow}/runs?{query}")
    # The query narrows the list; the checks below are what the gate relies on.
    return any(
        run.get("head_sha") == sha
        and run.get("head_branch") == BASE_BRANCH
        and run.get("conclusion") == "success"
        and run.get("event") in TRUSTED_EVENTS
        for run in data.get("workflow_runs", [])
    )


def check_runs(fetch: Fetch, repo: str, sha: str, workflows: list[str]) -> list[str]:
    problems: list[str] = []
    for workflow in workflows:
        try:
            ok = has_successful_run(fetch, repo, workflow, sha)
        except ApiError as error:
            problems.append(f"cannot read the runs of {workflow}: {error}")
            continue
        if not ok:
            problems.append(f"{workflow} has no successful run on {BASE_BRANCH} for commit {sha}")
    return problems


def verify(
    *,
    sha: str,
    repo: str,
    tag: str | None = None,
    base_ref: str = f"origin/{BASE_BRANCH}",
    root: Path = ROOT,
    fetch: Fetch = gh_api,
) -> list[str]:
    """Return every reason the commit must not be published (empty means it may be)."""
    problems = check_tag(tag) if tag else []
    problems += check_on_base(sha, base_ref, root)
    if SHA_PATTERN.match(sha):
        problems += check_runs(fetch, repo, sha, required_workflows(root))
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--sha", required=True, help="full commit SHA to release")
    parser.add_argument("--repo", required=True, help="owner/name of the repository")
    parser.add_argument("--tag", help="release tag; omit on a build-only dispatch")
    parser.add_argument("--base-ref", default=f"origin/{BASE_BRANCH}")
    args = parser.parse_args(argv)

    problems = verify(sha=args.sha, repo=args.repo, tag=args.tag, base_ref=args.base_ref)
    for problem in problems:
        print(f"verify_release: {problem}", file=sys.stderr)
    if problems:
        print(f"verify_release: {len(problems)} problem(s), do not publish", file=sys.stderr)
        return 1
    print(f"verify_release: ok ({args.sha} on {args.base_ref}, required runs green)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
