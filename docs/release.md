# Release

This page starts with the verified state of the repository boundary and will grow into the release procedure when the publish pipeline lands (step M1.7).

## Who releases

Only the maintainer creates `v*` tags, and a tag push is what publishes images. Agents never create tags or GitHub Releases, run `gh workflow run` or deploy (see ADR-0013).

## Repository boundary: verification record

Interim setup (Q20): no dedicated agent account, fork or separate agent workspace yet. Verified on 2026-10-26 from the maintainer's workspace (account `mano8`).

- **Ruleset `main`** (id 25856923, active, targets `refs/heads/main`, **no bypass**): deletion and non-fast-forward blocked; linear history; a pull request is required with 0 approvals, stale approvals are dismissed on push, review threads must be resolved, and the merge method is squash only. No required checks yet (added in H1.8).
- **Ruleset `release-tags`** (id 25856925, active, targets `refs/tags/v*`): creation, update and deletion restricted; the bypass is the repository **admin** role only. In the interim the agents' account is that admin, so only `CLAUDE.md` and the `.claude/settings.json` deny rules keep agents from tagging.
- **Repository merge settings:** squash only (merge commits and rebase merges are off); forking allowed; visibility public.
- **Actions:** the default workflow token is read-only; "Allow GitHub Actions to create and approve pull requests" is off; fork-run approval is set to first-time contributors. Public repositories never send write tokens or secrets to fork pull-request workflows, so no separate toggle exists for them.
- **GHCR:** no packages yet; they are created private at the first publish (M1.7, H8.6).
- **Checks:**
  - `git fetch origin` works;
  - the probe branch `h0.2-verify` was pushed;
  - draft PR #1 was opened, closed and its branch deleted;
  - a direct push to `main` was refused (GH013, "Changes must be made through a pull request");
  - a force-push to `main` was refused (GH013, "Cannot force-push to this branch");
  - `origin/main` was unchanged at `ceb0302`.
- **Still deferred (Q20):** the agent machine account, the fork, the separate workspace, and the move to 1 required approval.

## Release procedure

To be written in M1.7: tag the merged commit on `main`, let `docker-publish.yaml` verify, build, scan, sign and publish, then review the GitHub Release.
