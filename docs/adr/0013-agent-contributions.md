# ADR-0013: Agent contributions: interim single-account setup, protected tags, signed releases

- **Status:** Accepted
- **Date:** 2026-10-26

## Context

AI coding agents implement the plan; the maintainer reviews, merges, releases and deploys. The target is a dedicated machine account with the Read role working from a fork. For now (Q20, decided in H0.1 on 2026-10-26) there is no such account.

## Decision

### Interim setup (in force)

- The repository is public on GitHub Free, so rulesets are available. No Pro or organization plan is needed.
- Agents use the `gh` and git credentials already configured in the maintainer's workspace. One phase is one branch is one draft PR, pushed to `origin`. Agents never push `main`.
- That account is a repository admin, so server-side permissions do not stop it. The hard rules are in `CLAUDE.md` and the `.claude/settings.json` deny rules. The `main` ruleset (no bypass) still refuses direct pushes, force-pushes and deletion.
- Agents never merge or approve a PR, create tags or releases, run `gh workflow run`, change repository settings, rulesets or secrets, or deploy. Commits and PRs carry no AI attribution.
- Branches pushed to `origin` run their own workflows with upstream's token. Any change under `.github/workflows/` needs the maintainer's explicit review. This is an accepted, temporary regression of the fork-based isolation.
- The `main` ruleset requires a PR with 0 approvals while agents use the maintainer's account (an author cannot approve their own PR). The maintainer's review before merging is a working rule.

### Target (deferred)

A machine account with the Read role, working from its fork over SSH, with a classic PAT for `gh` and a separate OS account or dev container. Moving to it changes only the push command, the deny rules and the approval count.

### Releases (unchanged by the interim setup)

`v*` tags are protected by a ruleset (administrator bypass only) and publishing runs only on a `v*` tag push. A release builds two images (app and backup), signs them (cosign keyless plus build-provenance attestation) and publishes a signed `release-manifest.json`.

## Consequences

- The maintainer is the only guard that matters until the machine account exists; `docs/release.md` records the verified boundary.
- Required checks are added to the `main` ruleset in H1.8.
