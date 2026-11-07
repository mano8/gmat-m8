# ADR-0017: The VPS decides only on the signed manifest; image packages are public

- **Status:** Accepted (the maintainer's decisions of 2026-11-06)
- **Date:** 2026-11-06

## Context

**Release notes.** Two lines of a release's notes drove the deploy tooling: `Irreversible migration:` made `deploy.sh` ask for the version to be typed and `recover.sh migrate-back` refuse, and `Old code works on the new schema:` was the only thing that allowed `recover.sh rollback-images`. The notes are not signed. Anyone with write access to the repository's releases could delete the first, or add the second and let old code run on a new schema, while the manifest beside them is checked against the publish workflow's signature.

**Image access.** The plan had private GHCR packages and a fine-grained read-only token on the VPS. GHCR takes only classic personal access tokens: a fine-grained one logs in and is then denied on the pull. A classic token cannot be limited to one repository, and it expires, which would stop a deploy or a recovery at the pull. The repository is public, so the images hold nothing that the source does not.

## Decision

**The statements move into the signed manifest.** `release_tools.py manifest` copies them from the version's `CHANGELOG.md` section at the tag into `migration` (`irreversible`, `old_code_works`). `deploy.sh` and `recover.sh` read them only from there. A manifest that does not state them is refused, not read as "no". The notes stay for people to read and drive nothing.

**Both image packages are public, and the VPS holds no GitHub token.** GHCR creates them private at the first publish, so the maintainer changes their visibility before the first deploy. It cannot be undone. `GITHUB_READ_TOKEN` stays available, empty by default, for a fork with private packages: there it must be a classic token with only `read:packages`.

## Consequences

- Nothing on the VPS decides on text that the publish workflow did not sign.
- One credential less on the server, and no expiry that can block a recovery.
- The images are readable by anyone, as the source already is. Nothing else about them changes: the VPS still verifies every signature and pulls by digest.
- This amends plan §1 (private packages), H8.6 (the token) and F79.
