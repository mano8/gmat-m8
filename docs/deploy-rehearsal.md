# Deploy and recovery rehearsal

The record of plan step M8.4's "Done when": `deploy.sh`, `recover.sh`, the restore drill and the offsite rules run against real containers before they are ever used on the VPS. Run it again after changing any of them:

```bash
sg docker -c "REHEARSAL_REQUIRED=1 uv run pytest tests/rehearsal -m rehearsal -s -v"
```

It takes about 15 minutes once the images are cached and leaves only Docker's build cache behind. Run nothing else next to it on a small machine (see "What the rehearsal found").

## The last run

| | |
| --- | --- |
| Commit | `1e35ccf` on `m8-hardening` |
| Date | 2026-11-05 |
| Result | **37 passed in 14 min 10 s**; 38 checks of the drill script passed, none failed. `tests/live` against the rebuilt local stack: 29 passed |
| Machine | Linux, 2 cores, 16 GB, Docker 29.9.3, Compose 5.7.0 |
| restic | 0.18.1, in the backup image |

## What is real and what is not

Real: both images built from the checkout and pulled by digest from a registry; PostgreSQL, Caddy, Gunicorn, the worker and the backup in their production overlay; TLS and the maintenance gate through Caddy; `pg_dump`, `pg_restore`, the role bootstrap; restic against a REST server in `--append-only` mode; Mailpit counting what the worker sent; sessions, links and codes tried over HTTPS.

Stand-ins, because they cannot exist outside the release pipeline or the VPS:

- **Keyless signatures.** Only a tag push in GitHub Actions can produce one. A stand-in `cosign` on PATH knows what was "signed" and checks the identity and issuer it is given. Both refusals below run the **real cosign** (v2.5.0), so the positive path of a real signature is first proven by the publish workflow's own verification step and then by the first real deploy (H8.7).
- **GitHub's API.** A small local server answers `GITHUB_API_URL`. The signature decides what is trusted, wherever the files came from.
- **The registry** is local, plain HTTP; `DEPLOY_IMAGE_PREFIX` names it. `ghcr.io` login is not exercised.
- **The certificate** is Caddy's internal CA for `localhost`; ACME needs a real domain (H8.6).
- **The S3 option** of the offsite storage is not exercised; the REST server option is.

The scripts have no switch for any of this: a rehearsal differs from production only in PATH and in the environment variables `ops/common.sh` documents.

## Results

Each row of the plan's list, with what the run showed.

| Plan M8.4 | Shown |
| --- | --- |
| vN → vN+1 | First deploy from an empty database (gate window 35 s), then 1.0.0 → 1.1.0 with a migration (26 s), inside the 1–3 minute target of Q21. A real database, larger dumps and a slower VPS will take longer. Dump recorded, its offsite copy listed, new digests running, worker processing |
| Forced smoke failure after migrate | A release whose `/readyz/` fails but whose `/healthz/` answers, so only `verify-internal` can notice. It stops at step 9 with both gates on. For 100 s, past the moment a queued notification fell due: GET and POST answer the 503 page, the worker's heartbeat stays `paused`, Mailpit receives nothing, no audit row or attempt is added. `recover.sh migrate-back` starts the previous digests with the schema back; the held message is then delivered |
| Forced migration failure | A migration that fails after another that succeeded: stops at step 7. `recover.sh migrate-back`: audit count, newest audit event, attempts, users and materials all identical |
| Other rows of the decision tree | Failed at step 6: `recover.sh rollback`, no migration ran. A release flagged irreversible: the deploy asks for the version, `migrate-back` refuses, `restore-predeploy` brings every row back. Old code on the new schema: `rollback-images` starts the previous digests |
| Unsigned manifest or image refused | **The real cosign** refuses an unsigned `release-manifest.json`, and an unsigned image in the registry. Gates off, site open, nothing recorded |
| Two simultaneous deploys; a recovery waits | The second deploy waits for the lock and runs after the first. A recovery started during a deploy waits, then reads how that deploy ended and recovers it |
| Restore and reconciliation | Taken before the backup and refused after the restore, over HTTPS: the session cookie, the invitation link, the password-reset link; in the app, a signed form key. **Recovery codes**: one consumed before the backup, one unused at the backup, one regenerated after it: all refused. TOTP still works (MFA keys are kept) and the privileged account must generate new codes before anything else |
| A post-snapshot revocation and MFA reset | A membership revoked and a factor reset after the backup are applied again. A password changed after it: neither the old nor the new one signs in, the owner resets |
| A restored queued supplier dispatch | A queued dispatch and notification come up `QUARANTINED`. A dispatch sent after the backup takes `SENT` from the failed database. The operator lists and discards them (`manage outbox_quarantine`) |
| The failed state preserved first | A dump, the audit export, the re-entry list and a renamed copy of the `pg_data` volume, and the decision in `incident.log` |
| VPS credentials cannot delete a snapshot | `restic forget` and `forget --prune` answer **403** from the append-only server; every snapshot is still listed and `restic check` passes. restic itself exits 0 here, so the 403 and the listing are the proof |
| Forged snapshots | A future timestamp, an unknown host, an unknown tag and 13 snapshots in one day each make the prune refuse, with nothing removed |
| `--keep-within` keeps the protected window | 12 forged future snapshots really do push an older real one out of `--keep-monthly`, the attack of plan §9, while `--keep-within 90d` keeps every snapshot of the window. After a legitimate prune every snapshot of the last 90 days is there and the ones beyond the monthly rule are gone |
| No way out of the drill stack | Connections from `web` and from the worker to port 443 and 587 outside, by address and by name, all fail |
| Offsite drill that sends nothing | On its own project, from the offsite snapshot: checksum and row counts match the manifest, `SECRET_KEY` rotated, reconciled, runtime role writes, audit trigger blocks UPDATE and DELETE, an attachment matches its checksum, no session, invitation, email change or recovery code left, the queued dispatch held, no new attempt. Without a preserved database privileged accounts come up suspended; with a later dump standing for it, a revocation in that dump is applied again |
| Loss of the VPS (beyond the list) | `recover.sh restore-offsite` on a fresh project with empty volumes: the snapshot is fetched, reconciled with no failed state, privileged accounts suspended but the operator, and confirmed one by one with `manage confirm_account` |

The migration reversibility check the step also names was already in CI's `test` job (`scripts/check_migrations_reversible.py`): to zero and back passes through any earlier head. `tests/unit/config/test_workflows.py` now pins it.

## What the rehearsal found

Each was fixed before this record, and the fakes in `tests/unit/docker` could not have shown them:

- **BusyBox `flock` has no `-w`**: `backup-now --wait` failed in the Alpine image, so every deploy stopped at step 6.
- **A form post behind the gate got 405, not the maintenance page**: Caddy's file server answers only GET and HEAD. Nothing reached Django, but plan §9 promises the page for every request. Fixed for the gate and for the fallback page, and pinned in `tests/live`.
- **restic exits 0 when a server refuses a removal.** The prune script would have printed "Pruned." with append-only credentials; it now lists the repository again.
- **`--keep-within` is not measured from the newest snapshot**, as this tooling's first draft assumed: restic 0.18.1 measures from now. The real attack is the one plan §9 describes, on the weekly and monthly rules, and the rehearsal now demonstrates it.
- **restic retries an unreachable repository for about 15 minutes**, which step 6 would have spent with the site closed. `deploy.sh` now asks the repository before the gates.
- **A changed Caddyfile did not apply**: it is a bind mount, so `docker compose up` sees nothing new and Caddy keeps the old configuration. `tests/live` showed it. A deploy now restarts Caddy behind the gates and confirms the 503 again.
- **On a host short of memory one `docker compose exec` took 20 minutes** (load average 150, swap full), which used up a 90-second wait and closed the gates on a healthy deploy. Each wait now bounds its calls and allows a minimum number of tries. A VPS of 2 vCPU and 4 GB could meet the same; watch for it.

And from the tests against fakes, before the first real run: inside `cmd || die` bash ignores `set -e`, so a failed step in a function was passed over, and an unreadable migration head could have migrated every app back to zero.

## Not settled here

- The first real signature, ACME certificate, GHCR pull and offsite storage are H8.6 and H8.7.
- `cosign` and `python3` must be on the VPS; `docs/runbooks/vps-setup.md` (M8.5) says how.
- `docker compose` on the host reads `.env`, which holds `GITHUB_READ_TOKEN` and the offsite credentials: mode 0600, as `deploy.sh` checks.
