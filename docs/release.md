# Release

How a version of gmat-m8 becomes two signed images and a GitHub Release, who does what, and how to check the result. The state of the repository boundary (rulesets, Actions settings) is recorded further down.

## Who releases

Only the maintainer creates `v*` tags, and a tag push is what publishes images. Agents never create tags or GitHub Releases, run `gh workflow run` or deploy (see ADR-0013).

## What the pipeline does

`.github/workflows/docker-publish.yaml` runs on the push of a `v*` tag. A manual dispatch runs the same checks and builds but pushes, signs and releases nothing.

| Job | Does | Fails when |
| --- | --- | --- |
| `verify` | `scripts/verify_release.py` and `release_tools.py notes` | the tag is not `vX.Y.Z` (or `vX.Y.Z-pre.N`); `CHANGELOG.md` has no non-empty `## [X.Y.Z]` section; the commit is not an ancestor of `origin/main`; `ci.yaml`, `compose-smoke.yaml` (and `e2e.yaml` once that file exists at the commit, M8.2) have no successful run on `main` with `head_sha` equal to the commit |
| `scan` (app and backup, amd64 and arm64: four legs, read-only token) | builds the image, Trivy gate (HIGH and CRITICAL, no `--ignore-unfixed`, exceptions only from `.trivyignore.yaml`), an all-severity report, an SPDX SBOM | any scan finding not covered by an exception; a build error |
| `publish` (app, backup; tag push only, after every `scan` leg passed) | pushes the amd64 + arm64 index by digest to `ghcr.io/<owner>/gmat-m8` and `gmat-m8-backup`, runs the Trivy gate again on the **pushed digest** for both platforms, signs it with cosign (keyless), verifies that signature, attests build provenance | a finding in the pushed digest (the image stays unsigned, so nothing trusts it); any push, signature or attestation error |
| `release` (tag push only) | writes `release-manifest.json`, signs it (`cosign sign-blob`), verifies the signature, creates the GitHub Release with the `CHANGELOG.md` notes, the manifest and its bundle, the SBOMs and the Trivy reports | the manifest cannot vouch for an image (bad digest, a missing SBOM or scan report for either architecture, no exception file); the release already exists |

Nothing is signed or released unless every earlier step passed. Only the digests in the signed manifest are trusted; the `X.Y.Z` tag on GHCR is a label for humans.

`image-scan.yaml` runs every night (and on demand). It scans the released app and backup images (the digests in the latest release's manifest), Caddy and PostgreSQL (the digests in `docker-compose.yml`), each for amd64 and arm64, with the same gate; it keeps the full reports as artifacts for 90 days. When something is not covered it opens or updates one issue, "Nightly image scan: unresolved HIGH/CRITICAL findings". A later run in which every scan completed with no findings closes that issue with a comment; a run in which a scan could not complete leaves it alone. Before the first release only Caddy and PostgreSQL are scanned. An `image:` line in `docker-compose.yml` that is neither pinned by digest nor a `${VAR}` reference stops the run, so the scan cannot shrink silently.

## Release procedure (maintainer)

1. **Write the notes.** In a pull request, move the entries of `## [Unreleased]` in `CHANGELOG.md` under `## [X.Y.Z] - YYYY-MM-DD` and leave a fresh `## [Unreleased]` above it. Say there whether a migration is irreversible or needs a confirmation at deploy time. Merge it with the usual review and required checks.
2. **Wait for `main`.** `ci.yaml` and `compose-smoke.yaml` (and later `e2e.yaml`) run on the merge commit. Rehearse the gate on it without tagging anything (needs an authenticated `gh`):

   ```bash
   git fetch origin
   python scripts/verify_release.py --repo <owner>/gmat-m8 --sha "$(git rev-parse origin/main)" --tag vX.Y.Z
   ```

3. **Optional dry run.** Run `docker-publish` by hand (Actions tab, "Run workflow", branch `main`). It builds both images for both architectures and runs the Trivy gates and SBOMs; it pushes nothing. Do this before the first tag, and again after changing a Dockerfile or the workflow.
4. **Tag the commit** that passed step 2, annotated, and push the tag:

   ```bash
   git tag -a vX.Y.Z -m "vX.Y.Z" <sha>
   git push origin vX.Y.Z
   ```

   Never move or delete a published tag; fix forward with the next patch version.
5. **Watch the run**, then open the GitHub Release and check the list below.
6. **Deploy** on the VPS with `./deploy.sh X.Y.Z` (M8.4), which verifies everything in the next section before it touches anything.

A release has these files: `release-manifest.json`, `release-manifest.json.sigstore.json` (the signature bundle), `sbom-<image>-<arch>.spdx.json` (four) and `trivy-<image>-<arch>-report.json` (four, every severity, exceptions not applied).

## The manifest

```json
{
  "schema_version": 1,
  "version": "1.2.3",
  "tag": "v1.2.3",
  "commit": "<40 hex>",
  "repository": "<owner>/gmat-m8",
  "run_url": "https://github.com/<owner>/gmat-m8/actions/runs/<id>",
  "created_at": "2026-11-01T10:00:00Z",
  "images": {
    "app": {
      "repository": "ghcr.io/<owner>/gmat-m8", "tag": "1.2.3",
      "digest": "sha256:...", "ref": "ghcr.io/<owner>/gmat-m8@sha256:...",
      "platforms": ["linux/amd64", "linux/arm64"],
      "sboms": ["sbom-gmat-m8-amd64.spdx.json", "sbom-gmat-m8-arm64.spdx.json"],
      "scan": {"linux/amd64": {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 3, "LOW": 9, "UNKNOWN": 0}}
    },
    "backup": { "...": "same shape" }
  },
  "scan_gate": {
    "severity": "CRITICAL,HIGH", "ignore_unfixed": false,
    "ignorefile": ".trivyignore.yaml", "ignorefile_sha256": "<hex>"
  }
}
```

`scan` counts every finding of the full report, so accepted exceptions show up there; `ignorefile_sha256` ties the release to the exact exception list it was gated with.

## Verify a release by hand

The signing identity of every signature, for tag `vX.Y.Z`, is:

- certificate identity `https://github.com/<owner>/gmat-m8/.github/workflows/docker-publish.yaml@refs/tags/vX.Y.Z`;
- OIDC issuer `https://token.actions.githubusercontent.com`.

```bash
gh release download vX.Y.Z --repo <owner>/gmat-m8 --pattern 'release-manifest.json*'
cosign verify-blob \
  --bundle release-manifest.json.sigstore.json \
  --certificate-identity "https://github.com/<owner>/gmat-m8/.github/workflows/docker-publish.yaml@refs/tags/vX.Y.Z" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  release-manifest.json

# for each "ref" in the manifest:
cosign verify --certificate-identity "<same identity>" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com "<ref>"
gh attestation verify "oci://<ref>" --repo <owner>/gmat-m8
```

`deploy.sh` (M8.4) makes the first two checks and pulls both images by the digests in the verified manifest.

## When something fails

- **`verify` fails.** The message names the reason. A commit off `main` or without green runs is never published: merge it, wait for the runs, and tag the merge commit. A missing changelog section is a one-line pull request.
- **The Trivy gate fails.** Fix it (new base image digest, dependency bump) or accept it in a pull request the maintainer approves: an entry in `.trivyignore.yaml` with the component, the compensating control, the owner and an expiry at most 90 days away (`scripts/check_trivyignore.py` enforces the format). Then release the next version; never skip the gate.
- **Failure after the push** (signature, attestation, manifest). Images may already be on GHCR. No release exists, and nothing trusts a digest that is not in a signed manifest, so there is nothing to undo: fix the cause and use "Re-run failed jobs", or release the next patch version.
- **"release already exists".** The workflow never overwrites a release. Delete the release (not the tag) in the GitHub UI and re-run the `release` job, or release the next patch version.
- **First publish.** GHCR creates the packages private. They are linked to this repository through the `org.opencontainers.image.source` label, so the run's token can push and the nightly scan can pull. Package visibility and the VPS's read-only token are the maintainer's (H8.6).

## Pipeline verification record

Verified on 2026-10-29 from the maintainer's workspace, on branch `m1-release` (PR 2 of M1), before it was merged:

- `actionlint` 1.7.7 is clean on every workflow.
- `scripts/verify_release.py` was run against the live repository without a tag:
  - the branch's own commit (not on `main`): refused, for ancestry and for both missing runs, exit 1;
  - the initial commit (on `main`, no workflow runs) with a malformed tag `1.0.0`: refused, for the tag and both missing runs, exit 1;
  - the `main` tip `3a27300` (merge of PR 1): accepted, `ci.yaml` and `compose-smoke.yaml` have successful `main` runs on that SHA, exit 0.
- Policy tests (`tests/unit/config/test_workflows.py`) pin the triggers, the job permissions (write scopes only in the release jobs, none in `scan`), that `publish` and `release` run only on a tag push, that the pushed digest is gated for both platforms before it is signed, and the Trivy gates of both images and both architectures.

Still to do by the maintainer, after PR 2 is merged: the build-only dispatch of `docker-publish` on `main` (step 3 above; agents do not run it), and H1.8. Record the result here:

- Build-only dispatch: _not run yet_.

## Repository boundary: verification record

Interim setup (Q20): no dedicated agent account, fork or separate agent workspace yet. Verified on 2026-10-26 from the maintainer's workspace (account `mano8`).

- **Ruleset `main`** (id 25856923, active, targets `refs/heads/main`, **no bypass**): deletion and non-fast-forward blocked; linear history; a pull request is required with 0 approvals, stale approvals are dismissed on push, review threads must be resolved, and the merge method is squash only. No required checks yet (added in H1.8).
- **Ruleset `release-tags`** (id 25856925, active, targets `refs/tags/v*`): creation, update and deletion restricted; the bypass is the repository **admin** role only. In the interim the agents' account is that admin, so only `CLAUDE.md` and the `.claude/settings.json` deny rules keep agents from tagging.
- **Repository merge settings:** squash only (merge commits and rebase merges are off); forking allowed; visibility public.
- **Actions:** the default workflow token is read-only; "Allow GitHub Actions to create and approve pull requests" is off; fork-run approval is set to first-time contributors. Public repositories never send write tokens or secrets to fork pull-request workflows, so no separate toggle exists for them.
- **GHCR:** no packages yet; they are created private at the first publish (see above, H8.6).
- **Checks:**
  - `git fetch origin` works;
  - the probe branch `h0.2-verify` was pushed;
  - draft PR #1 was opened, closed and its branch deleted;
  - a direct push to `main` was refused (GH013, "Changes must be made through a pull request");
  - a force-push to `main` was refused (GH013, "Cannot force-push to this branch");
  - `origin/main` was unchanged at `ceb0302`.
- **Still deferred (Q20):** the agent machine account, the fork, the separate workspace, and the move to 1 required approval.
