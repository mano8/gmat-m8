# Deploy and recovery rehearsal

The record of plan step M8.4's "Done when": `deploy.sh`, `recover.sh`, the restore drill and the offsite rules run against real containers before they are ever used on the VPS. Run it again after changing any of them:

```bash
sg docker -c "REHEARSAL_REQUIRED=1 uv run pytest tests/rehearsal -m rehearsal -s -v"
```

It takes about 21 minutes once the images are cached, needs about **3 GB of memory free** at its peak, and leaves only Docker's build cache behind. Run nothing else heavy next to it, and start with about 6 GB free (see "What the rehearsal found").

## The last run

| | |
| --- | --- |
| Commit | `170d073` on `m8-hardening`; one commit after it, `worker` and `backup` are created after `web`, checked apart (below) |
| Date | 2026-11-06 |
| Result | **39 passed in 21 min 4 s**. 40 checks of the drill script over its two runs, none failed. Memory never went below 2.9 GB free, and nothing was killed. The live and browser suites run in CI, on the same stack |
| Machine | Linux, 2 cores, 16 GB with desktop applications open (about 6 GB free), Docker 29.9.3, Compose 5.7.0 |
| restic | 0.18.1, in the backup image |

## Which daemon

The production host runs Docker **rootless** ([ADR-0033](adr/0033-rootless-docker-host.md), [rootless-host.md](rootless-host.md)); CI and developers' machines run the **root** daemon, and the scripts behave as before under it. The rehearsal today runs on a root daemon: the command above uses `sg docker`, the group that the production host does not have, and it is a developer's machine, not the server. Nothing in the harness names the daemon: the scripts ask `docker info`.

- After M12.2's helper (`backups_do`), the rehearsal was run again on the root daemon: 40 passed in 18 min 22 s, the drill passed twice, the lowest free memory 8.1 GB (2026-11-24).
- The same run under a **rootless daemon** (`DOCKER_HOST` set to its socket, on a non-production x86-64 machine, never on the pilot host), with the measurements of Q75 (the client's address reaching Caddy, UDP 443 answered, a 10 MiB upload, `slirp4netns` and, if offered, `pasta`), belongs to M12.5 and is **not recorded here yet**. Until it is, the unit tests (`tests/unit/docker/`, a fake rootless daemon) and the real-daemon checks of [rootless-host.md](rootless-host.md) are what pin the rootless path.
- What a rootless run needs: the daemon of [runbooks/vps-setup.md](runbooks/vps-setup.md) §4.1 for the account that runs pytest (subordinate ids, `slirp4netns`, `nsenter`), and `DOCKER_HOST` or the `rootless` context in the shell that starts it.

## Q75: the port driver, measured

What [ADR-0033](adr/0033-rootless-docker-host.md) point 4 asked of M12.5, on the machine of the rootless run (2026-11-25; 2 vCPU, Docker 29.9.3, RootlessKit 3.3.0, slirp4netns 1.2.1, passt 2024-02-20). Caddy 2.11.9 (the stack's image) with HTTP/1.1, 2 and 3 answered its own `{remote_host}`, and passed a 10 MiB body on to a small server that read it to the end. The visitor was a container of the root daemon on a network of its own (`192.168.250.0/24`), reaching the host's address; HTTP/3 came from the host itself (aioquic 1.2.0). The daemon was restarted with each pair of drivers through the runbook's override file, then set back to `slirp4netns`.

| Daemon, network/port driver | Address Caddy saw over TCP (the visitor was `192.168.250.2`) | HTTP/3 (UDP) answered, address seen | 10 MiB over HTTP/2, median of 5 | 10 MiB over HTTP/3, median of 3 |
| --- | --- | --- | --- | --- |
| root (for comparison) | `172.18.0.1` (from the same host, through Docker's proxy) | yes | 0.058 s | 0.269 s |
| rootless, `slirp4netns`/`slirp4netns` (the runbook's) | **`192.168.250.2`** | yes, the host's own address | 0.070 s | 0.317 s |
| rootless, `pasta`/`implicit` | **`192.168.250.2`** | yes, the host's own address | 0.112 s | 0.281 s |
| rootless, `slirp4netns`/`builtin` (Docker's default) | `172.18.0.1`, the network's gateway | yes, but `172.18.0.1` | 0.062 s | 0.243 s |

- `slirp4netns` keeps the visitor's address and carries UDP 443: Q75's choice holds, and user-mode networking costs about 12 ms on a 10 MiB upload here.
- `builtin` hides the address over TCP too with Docker's defaults, so `deploy.sh` refuses it, which it did on this daemon, with its reason.
- `pasta` with `implicit` keeps the address too, but was slower here and publishes a port only on every host address (`-p <one address>:…` fails with "cannot assign requested address"). The maintainer refused it after this measurement, and `deploy.sh` now refuses it, saying why (checked on this daemon). `pesto` needs a newer `pasta` than Debian and Ubuntu ship.
- Two limits found on the way: HTTP/3 from the host's own loopback gets no answer under `slirp4netns` (TCP from there arrives from `10.0.2.2`, as on the pilot host); and a visitor whose address lies inside one of the daemon's own networks never gets an answer. The first visitor tried, a container at `172.17.0.3`, met the second: the rootless daemon's bridge is `172.17.0.0/16` inside its namespace too ([rootless-host.md](rootless-host.md)).
- Not measured here: a visitor on another machine (the pilot host checks it from outside, vps-setup.md §8), and ufw turned on.

## What is real and what is not

Real: both images built from the checkout and pulled by digest from a registry; PostgreSQL, Caddy, Gunicorn, the worker, the backup, ClamAV's `clamd` and `freshclam` and the decoder in their production overlay (`verify-internal` asks the scanner and the decoder for an answer; a real upload through them is `tests/live/test_attachments.py`); TLS and the maintenance gate through Caddy; `pg_dump`, `pg_restore`, the role bootstrap; restic against a REST server in `--append-only` mode; Mailpit counting what the worker sent; sessions, links and codes tried over HTTPS.

Stand-ins, because they cannot exist outside the release pipeline or the VPS:

- **Keyless signatures.** Only a tag push in GitHub Actions can produce one. A stand-in `cosign` on PATH knows what was "signed" and checks the identity and issuer it is given; asked `cosign version`, it answers v3.0.6, the version the runbook installs, which `deploy.sh` checks before it fetches a release. Both refusals below run the **real cosign** (rehearsed with v2.5.0; a refusal holds with any version, but a real deploy needs v3.0.6 or newer to find the release's signatures, see [runbooks/vps-setup.md](runbooks/vps-setup.md)), so the positive path of a real signature is first proven by the publish workflow's own verification step and then by the first real deploy (H8.7).
- **GitHub's API.** A small local server answers `GITHUB_API_URL`. The signature decides what is trusted, wherever the files came from.
- **The registry** is local, plain HTTP; `DEPLOY_IMAGE_PREFIX` names it. `ghcr.io` login is not exercised.
- **The certificate** is Caddy's internal CA for `localhost`; ACME needs a real domain (H8.6).
- **The S3 option** of the offsite storage is not exercised; the REST server option is.

The scripts have no switch for any of this: a rehearsal differs from production only in PATH and in the environment variables `ops/common.sh` documents.

## Results

Each row of the plan's list, with what the run showed.

| Plan M8.4 | Shown |
| --- | --- |
| vN → vN+1 | First deploy from an empty database (gate window 57 s), then 1.0.0 → 1.1.0 with a migration (45 s), inside the 1–3 minute target of Q21; each now waits for `clamd` to load its signatures. A real database, larger dumps and a slower VPS will take longer. Dump recorded, its offsite copy listed, new digests running, worker processing |
| Forced smoke failure after migrate | A release whose `/readyz/` fails but whose `/healthz/` answers, so only `verify-internal` can notice. It stops at step 9 with both gates on. For 100 s, past the moment a queued notification fell due: GET and POST answer the 503 page, the worker's heartbeat stays `paused`, Mailpit receives nothing, no audit row or attempt is added. `recover.sh migrate-back` starts the previous digests with the schema back; the held message is then delivered |
| Forced migration failure | A migration that fails after another that succeeded: stops at step 7. `recover.sh migrate-back`: audit count, newest audit event, attempts, users and materials all identical |
| Other rows of the decision tree | Failed at step 6: `recover.sh rollback`, no migration ran. A release flagged irreversible: the deploy asks for the version, `migrate-back` refuses, `restore-predeploy` brings every row back. Old code on the new schema: `rollback-images` starts the previous digests |
| Unsigned manifest or image refused | **The real cosign** refuses an unsigned `release-manifest.json`, and an unsigned image in the registry. Gates off, site open, nothing recorded |
| Two simultaneous deploys; a recovery waits | The second deploy waits for the lock and runs after the first. A recovery started during a deploy waits, then reads how that deploy ended and recovers it |
| Restore and reconciliation | Taken before the backup and refused after the restore, over HTTPS: the session cookie, the invitation link, the password-reset link; in the app, a signed form key. **Recovery codes**: one consumed before the backup, one unused at the backup, one regenerated after it: all refused. TOTP still works (MFA keys are kept) and the privileged account must generate new codes before anything else |
| A post-snapshot revocation and MFA reset | A membership revoked after the backup is revoked again. A factor reset after it keeps the account suspended until a human accepts it (ADR-0016), and the factors then go. A password changed after it: neither the old nor the new one signs in, the owner resets |
| A restored queued supplier dispatch | A queued dispatch and notification come up `QUARANTINED`. A dispatch sent after the backup takes `SENT` from the failed database. The operator lists and discards them (`manage outbox_quarantine`) |
| The failed state preserved first | A dump, the audit export, the re-entry list and a renamed copy of the `pg_data` volume, and the decision in `incident.log` |
| VPS credentials cannot delete a snapshot | `restic forget` and `forget --prune` answer **403** from the append-only server; every snapshot is still listed and `restic check` passes. restic itself exits 0 here, so the 403 and the listing are the proof |
| Forged snapshots | A future timestamp, an unknown host, an unknown tag and 13 snapshots in one day each make the prune refuse, with nothing removed. So does a snapshot **dated months back with the right tags**, new since the last prune: what that prune saw is kept on the maintainer's machine, out of the VPS's reach |
| `--keep-within` keeps the protected window | 12 forged future snapshots really do push an older real one out of `--keep-monthly`, the attack of plan §9, while `--keep-within 90d` keeps every snapshot of the window. After a legitimate prune every snapshot of the last 90 days is there and the ones beyond the monthly rule are gone |
| No way out of the drill stack | Connections from `web` and from the worker to port 443 and 587 outside, by address and by name, all fail |
| Offsite drill that sends nothing | On its own project, from the offsite snapshot: checksum and row counts match the manifest, `SECRET_KEY` rotated, reconciled, runtime role writes, audit trigger blocks UPDATE and DELETE, an attachment matches its checksum, no session or row of the session list, invitation, email change or recovery code left, the queued dispatch held, no new attempt. Without a preserved database privileged accounts come up suspended; with a later dump standing for it, a revocation in that dump is applied again |
| Loss of the VPS (beyond the list) | `recover.sh restore-offsite` on a fresh project with empty volumes: the snapshot is fetched, reconciled with no failed state, privileged accounts suspended but the operator, and confirmed one by one with `manage confirm_account` |
| A database that does not start (beyond the list) | With `PG_VERSION` really removed, `recover.sh restore --db-unreadable` asks first, from the dump's own time, waits for nothing on the broken cluster, keeps its volume under a new name, restores into an empty one, and the site opens |

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
- **The rehearsal itself could run a desktop out of memory.** With about 6 GB free and two stacks up at once (two `clamd`, about 1 GB each), the kernel killed desktop applications twice in one run. The test that starts a second stack now stops the first one's `clamd`, and the next run stayed above 2.9 GB free from the same start, in 21 minutes. A production server runs one `clamd`, and the drill stack starts none.
- **A new `media` volume could stop the stack from starting.** Docker copies the image's `/srv/media` into a new volume when a container is created, and two created at once can both try: the loser fails with "file exists". CI hit it about once in 40 runs. `worker` and `backup` now depend on `web`, so it is created first. It could not be reproduced on demand, so what is checked is the order: `web` was created before the others in 18 of 18 tries, for the whole stack and with `--no-deps`.

And from the tests against fakes, before the first real run: inside `cmd || die` bash ignores `set -e`, so a failed step in a function was passed over, and an unreadable migration head could have migrated every app back to zero.

## Not settled here

- The first real signature, ACME certificate, GHCR pull and offsite storage are H8.6 and H8.7.
- `cosign` and `python3` must be on the VPS; [runbooks/vps-setup.md](runbooks/vps-setup.md) says how.
- `docker compose` on the host reads `.env`, which holds the offsite credentials: mode 0600, as `deploy.sh` checks.
- The rehearsal under a rootless daemon and the real offsite fetch on the pilot host: M12.5 and H12.6.
