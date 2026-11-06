# Deploy and roll back

How a release reaches production, and what to do when it does not go well. Everything runs on the VPS, as the `deploy` user, from `docker_compose/gmat_m8/` in a checkout of the release's tag. Only the maintainer deploys. The tooling is `deploy.sh` and `recover.sh`; their headers and [../release.md](../release.md) have the details, and [../deploy-rehearsal.md](../deploy-rehearsal.md) shows what was rehearsed against real containers.

The rule behind all of it: **a human decides, the script executes.** A failed deploy stops with both gates on (the public site answers the 503 maintenance page and the worker sends nothing), so there is time to read and think. Nothing in this page is urgent enough to skip that.

## Before a deploy

1. The release exists: the tag `vX.Y.Z` was pushed by the maintainer, `docker-publish` finished, and the GitHub Release has `release-manifest.json` and its `.sigstore.json` bundle ([../release.md](../release.md)).
2. Read the release notes (the `## [X.Y.Z]` section of `CHANGELOG.md`). Look for two lines, which the signed manifest carries too; the scripts go by the manifest:
   - `Irreversible migration: …` — going back will mean restoring the pre-deploy dump. `deploy.sh` makes you type the version to continue.
   - `Old code works on the new schema: …` — the only thing that allows `rollback-images` after the commit point.
3. Pick a time **outside working hours**. The window is about 1–3 minutes of maintenance page; a release with a large migration takes longer.
4. Check the machine:

   ```bash
   df -h /                                   # room for the images, a dump and a possible preserved copy
   ./recover.sh status                       # gates off, last deploy finished
   docker compose -f docker-compose.yml -f docker-compose.production.yml ps    # everything healthy
   ```

   If a gate is on or the last deploy did not finish, do **not** start another one: decide with [the decision tree](#the-decision-tree) first, or pass `--resume` on purpose when the new release is the fix (see below).

## Deploying

```bash
cd ~/gmat-m8
git fetch --tags && git switch --detach vX.Y.Z      # the compose files must be the release's own
cd docker_compose/gmat_m8
./deploy.sh X.Y.Z
```

`deploy.sh` has no flag that skips a signature check. What it does, in order:

| Step | What | If it fails |
| --- | --- | --- |
| 1 | Takes the deploy lock (`/var/lock/gmat-m8-deploy`), shared with `recover.sh` and the drill. Checks the tools, the env files (0600), the admin allowlist, the offsite repository setting and the checkout. | Nothing changed. Fix and run again. |
| 2 | Downloads the manifest, verifies its **signature** and the signature of both images against the workflow identity of that tag, checks version, repository and registry, pulls both images **by digest**. | Nothing changed, the site stayed open. |
| 3 | Preflight: pulls the third-party images (Caddy, PostgreSQL, ClamAV), then with the new image, read-only: the pending migrations (`migrate-plan`), `check --deploy`, the offsite repository answers. | Nothing changed. |
| 4 | **Gates on**: `flags/maintenance.on` and `flags/worker.paused`. Confirms from outside that Caddy answers 503. | Gates stay on. |
| 5 | Stops `web` and `worker`. | Gates stay on. |
| 6 | Pre-deploy backup (`pre-deploy-X.Y.Z`) with its offsite copy. The dump id is recorded. | Gates stay on; no migration ran. |
| 7 | Migrates, as the owner role. | Gates stay on. The migration may have partly run. |
| 8 | Starts the scanner (`clamd`, recreated so a changed configuration applies; loading its signatures takes about a minute), `freshclam`, the `decoder`, then `web`, `worker`, `backup` on the new digests. Traffic is still gated, the worker only reports ready. | Gates stay on. |
| 9 | `verify-internal` against `web:8000`: health, readiness, login page, a static file, that the malware scanner and the image decoder answer, `check --deploy`, `migrate --check`, the paused worker's heartbeat. | Gates stay on. |
| 10 | **Commit point**: removes `worker.paused`, then `maintenance.on`. Checks from outside that `/healthz/` and the login page answer 200 and the worker is processing. If either fails, both gates go back on. | Gates back on. |
| 11 | Records version, both digests, dump id and time in `deployed.log`. | — |

After it finishes, **look at the site**: sign in, open the dashboard, check `docker compose logs --since 10m web worker` for errors, and `./recover.sh status` (gates off). Then send the "deployed" note to whoever needs it.

The first deploy ever has no previous release, so there is nothing to roll back to: if it fails, fix the cause and deploy again.

## When a deploy stops

The script prints the matching row of the decision tree. You can see it again at any time:

```bash
./recover.sh status
```

It shows the running release, both gates, the step where the last deploy stopped and the pre-deploy dump.

### The decision tree

| Where it stopped | What happened | Do | Data loss |
| --- | --- | --- | --- |
| Before step 4 (before the gates) | Verification, preflight or a missing tool. The site never closed | Read the message, fix the cause, run `./deploy.sh X.Y.Z` again | None |
| Steps 4–6 | Gates on; no migration has run | `./recover.sh rollback` — starts the **previous digests**, verifies, commit point. Or fix the cause and `./deploy.sh X.Y.Z --resume` | None |
| Steps 7–9 and the release is **reversible** | Migrations ran (maybe partly) | `./recover.sh migrate-back` — migrates back to the previous head with the new image, then starts the previous digests | None: nothing was written since step 5 |
| Steps 7–9 and the release is **irreversible** (or `migrate-back` refuses) | | `./recover.sh restore-predeploy --operator EMAIL` — keeps the failed state, restores the pre-deploy dump, reconciles, starts the previous digests | None: nothing was written since step 5 |
| After the commit point: a defect | Users are working | **Forward-fix first**: merge a fix, release `X.Y.Z+1`, `./deploy.sh X.Y.Z+1`. Use `./recover.sh rollback-images` only if the signed manifest states `Old code works on the new schema` | None |
| After the commit point: only a restore can fix it | Data is wrong in a way no code fixes | `./recover.sh restore DUMP --operator EMAIL --incident "TEXT"` — **an explicit human decision**, see below | Everything written since the deploy, unless re-entered |
| The VPS is gone | | [backup-restore.md](backup-restore.md#the-vps-is-gone) | Up to the RPO (6 h) |

`EMAIL` is a **platform operator** (an account created with `bootstrap_admin --superuser`, see [user-admin.md](user-admin.md#the-first-administrator)), because a restore runs reconciliation as one.

Notes that apply to every row:

- Every action except `status` takes the deploy lock, puts both gates on, stops the writers and ends with the internal verification and the commit point. A failure leaves both gates on: the site shows the maintenance page and no email is sent. Read the output, fix, run the action again.
- Actions that destroy something ask you to type `RESTORE` (or pass `--confirm RESTORE` when there is no terminal). Read the question it prints first: it names the dump and states the loss window.
- Prefer the smallest action that works. **Never** delete the gate files by hand to "get the site back" while a deploy is unresolved: that is how a half-migrated database starts taking writes.
- A fix-forward release after a failed deploy needs `./deploy.sh X.Y.Z+1 --resume`. Both gates stay on until its own commit point, and it goes back to the dump taken before the first failed attempt. If the fix fails too, even at its first step, `./recover.sh status` still knows the failed release migrated, so `rollback` stays refused and the row is the migration's.

### Rolling back the images after the commit point

Only when the release's signed manifest says the old code works on the new schema:

```bash
./recover.sh rollback-images
```

It starts the previous digests with the new schema, behind the gates. Users keep every write made since the deploy. If it does not say so, the script refuses, and **do not** work around it: old code on a changed schema can corrupt data. Forward-fix.

### Restoring after writes were accepted

This loses data, so it is a decision, not a reflex:

1. Write down what is wrong, who found it, and why a fix-forward cannot work.
2. Pick the dump (list them with `docker compose exec backup restore --list`, newest first, or `ls backups/`). The pre-deploy dump is `gmat-<stamp>-pre-deploy-X.Y.Z`.
3. Run:

   ```bash
   ./recover.sh restore gmat-<stamp>-pre-deploy-X.Y.Z --operator you@example.org --incident "short reason"
   ```

   First it states the **loss window** (everything written since that dump was taken) and asks you to type `RESTORE`; answering anything else leaves the site as it was. Then it closes both gates and **preserves the failed state** under `preserved/<time>/`: a dump, the outbox outcomes, the access and credential events, the documents written since the dump (for re-entry), and a renamed copy of the `pg_data` volume.
4. After it ends, follow [the post-restore steps](backup-restore.md#after-any-restore) (quarantined email, suspended accounts), and tell the people who lost work what to re-enter, using `preserved/<time>/written-since-deploy.json`.
5. Fill in [incident.md](incident.md)'s record.

`preserved/` holds personal data. Delete it when the incident is closed (`rm -rf preserved/<time>` and `docker volume rm` the renamed volume).

## After a deploy

- `deployed.log` has the new line. The old images stay on disk for `rollback`; `docker image prune` only after you are sure.
- The CHANGELOG already has the release notes; nothing to edit on the server.
- If the release changed **Caddy**, the Caddyfile or the maintenance page, the deploy restarted Caddy behind the gates, so it already applies.
- If anything about the release surprised you, write it in the incident log or the next release's notes.

## Emergency: the site is down and no deploy is running

Different from a failed deploy. Check, in this order:

```bash
docker compose -f docker-compose.yml -f docker-compose.production.yml ps
docker compose -f docker-compose.yml -f docker-compose.production.yml logs --since 15m caddy web db
./recover.sh status
df -h /
```

- Gate flags present (`ls flags/`)? Someone paused it; see `status` and [incident.md](incident.md).
- A container restarting? Read its log; most often a full disk or an env file mistake.
- Only `web` unhealthy: `docker compose ... restart web`. Do not restart `db` unless the logs say to.
- Uploads refused with "cannot be checked right now" and the rest of the site fine: `clamd` or `decoder` is down. Read its log, `docker compose ... up -d --no-deps clamd decoder`. `clamd` takes about a minute to load its signatures. If it was killed for memory (`docker inspect --format '{{.State.OOMKilled}}'`), the server is too small for what it does: see the sizing in [vps-setup.md](vps-setup.md).
- Certificate error: Caddy could not renew; `docker compose logs caddy` names the reason (DNS, port 80 blocked, rate limit).

Then follow [incident.md](incident.md).
