# ADR-0026: Every change answers the ADRs that govern what it touches

- **Status:** Accepted (the maintainer's request, issue #48)
- **Date:** 2026-11-08
- **Governs:** `scripts/check_adr_alignment.py`, `.github/workflows/adr.yaml`, `.github/pull_request_template.md`, `.claude/settings.json`

## Context

An ADR wins over any plan (`CLAUDE.md`, "Source of truth"), but nothing kept the ADRs in line with the code. A change could contradict a recorded decision, or make a new one, without anyone writing it down, and an agent session learned of an ADR only if it happened to read it. Over time the ADRs would describe a system that no longer exists, and the rule that they win would point to the wrong answer.

## Decision

- **Each ADR names the files it governs** on a `- **Governs:**` line in its header: paths or globs in backticks, a directory ending in `/` for everything below it, or `none` for a decision with no file of its own. A file may be governed by several ADRs.
- **A change answers every ADR whose governed files it touches**, in the same pull request: it updates the ADR (or supersedes it with a new one) when it alters, extends or contradicts the decision; otherwise the pull request's `## ADRs` section names the ADR and says why it still holds. A new decision with no ADR gets one.
- **Three places enforce it**, from `scripts/check_adr_alignment.py`:
  - *While editing:* a Claude Code `PostToolUse` hook (`.claude/settings.json`) tells the session, after each edit of a governed file, which ADRs govern it.
  - *At pull request time:* the `adr-alignment` check (`.github/workflows/adr.yaml`) fails while an ADR whose governed files changed is neither changed nor named in the `## ADRs` section. It runs again when the description is edited. Dependabot's version bumps are exempt.
  - *Always:* `check` in the `lint` job: every ADR has a Governs line, every entry matches a tracked file (a move or rename that orphans one fails until the ADR follows), the numbers run without a gap, and `docs/adr/README.md` lists every ADR.

## Alternatives rejected

- **A rule in `CLAUDE.md` alone:** an agent that does not reread it, or a person who never read it, changes governed code without knowing; nothing fails.
- **Commit trailers instead of the description:** they cannot be added after the fact without rewriting history (force-pushes are forbidden here), and people rarely write them.
- **Checking whether the change agrees with the ADR's text:** not something a script can judge. The check makes sure someone looked; the review judges what they wrote.

## Consequences

- The mapping is a first proposal: it lists the files where each decision lives, not every file it touches. It is corrected like any ADR, in a pull request; a file that moves takes its entry along.
- A pull request touching governed files carries a `## ADRs` section; the template has it.
- `adr-alignment` becomes a required check only when the maintainer adds it to the `main` ruleset.
- The hook sees edits made with the file tools, not those made by shell commands; the pull request check is the backstop.
