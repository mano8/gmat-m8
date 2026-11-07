"""Policy test for the local agent guard, ``.claude/settings.json`` (CLAUDE.md "Never"; plan §0.1).

In the interim setup (Q20) agents work through the maintainer's account, which the tag
ruleset lets through, so these deny rules and CLAUDE.md are what keeps an agent from
merging, releasing, tagging or dispatching. A rule removed by mistake fails here.
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

REQUIRED_DENY = {
    # Merging, approving, releasing, dispatching, settings and secrets.
    "Bash(gh pr merge:*)",
    "Bash(gh pr review --approve:*)",
    "Bash(gh release:*)",
    "Bash(gh workflow run:*)",
    "Bash(gh repo edit:*)",
    "Bash(gh secret:*)",
    # `gh api` writes with an explicit method, and implicitly (a POST) as soon as it sends
    # fields or a body: a release, a tag ref or a workflow dispatch through the REST API.
    "Bash(gh api *-X*)",
    "Bash(gh api *--method*)",
    "Bash(gh api * -f*)",
    "Bash(gh api * -F*)",
    "Bash(gh api *--field*)",
    "Bash(gh api *--raw-field*)",
    "Bash(gh api *--input*)",
    # Tags publish images; main only changes through merged PRs; history is never rewritten.
    "Bash(git tag:*)",
    "Bash(git update-ref * refs/tags/*)",
    "Bash(git push --tags:*)",
    "Bash(git push * --tags:*)",
    "Bash(git push * refs/tags/*)",
    "Bash(git push * tag *)",
    "Bash(git push --force:*)",
    "Bash(git push * --force:*)",
    "Bash(git push origin main:*)",
    "Bash(git push --no-verify:*)",
    "Bash(git commit --no-verify:*)",
    "Bash(git add -f:*)",
}


def test_agent_guard_denies_every_publishing_and_history_path() -> None:
    settings = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    deny = set(settings["permissions"]["deny"])
    assert REQUIRED_DENY - deny == set()
    assert "allow" not in settings["permissions"]
