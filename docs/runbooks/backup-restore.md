# Backup and restore

What is backed up, how to know the backups work, how to restore after each kind of loss, and what to do afterwards. Targets (Q19): **RPO 0** for a failed deploy (writes are stopped), **RPO ≤ 6 h** and **RTO ≤ 4 h** for the loss of the VPS.

Commands run on the VPS in `docker_compose/gmat_m8/`, as `deploy`, unless a section says otherwise. In this page `dc` stands for the production compose invocation:

```bash
alias dc='docker compose -f docker-compose.yml -f docker-compose.production.yml'
```

## What exists

| Copy | Where | Cadence | Kept | Protects against |
| --- | --- | --- | --- | --- |
| Local dump, manifest and media copy | `backups/gmat-<UTC stamp>[-label].{dump,manifest.json,media/}` on the VPS | every 6 hours (00, 06, 12, 18 UTC) and a `pre-deploy-X.Y.Z` one per deploy | 3 days | a failed deploy, a bad migration, an operator's mistake |
| Offsite restic snapshot of the same files | the append-only repository ([../../docker_compose/offsite/README.md](../../docker_compose/offsite/README.md)) | after each dump | 90 days in full, then 4 weekly and 12 monthly | losing the VPS, or an attacker on it |
| Offline keys and env files | the maintainer's offline storage ([vps-setup.md](vps-setup.md#7-keep-the-keys-somewhere-else)) | when they change | — | everything above is useless without `RESTIC_PASSWORD` |

A dump is **snapshot-consistent**: the backup holds the advisory lock `gmat:media-backup`, takes one `REPEATABLE READ` snapshot and uses it for the dump, the manifest (row counts, newest audit event, migration head, app version, attachment list) and the copy of the attachment files. They always agree, even under concurrent writes. The worker purges deleted attachment files only while it can take the same lock, and only a day after their rows went, so a file in a snapshot cannot disappear during its copy. Sessions and the cache table are not in the dump.

The local copies are **not encrypted** (exception X12): `backups/` is mode 0700 and the files 0600. The offsite copies are encrypted by restic.

Dumps leave out the rows of `django_session`, `usersessions_usersession` and `django_cache`: session keys, the addresses and browsers of the session list, and rate-limit counters do not travel. The malware scanner's signatures are not backed up either; a new server gets them from the scanner's image and `freshclam`.

A backup holds second-factor secrets under the MFA key **of its day**. Keep a retired `MFA_ENCRYPTION_KEYS` key offline until the last backup that needs it has aged out, 90 days after you rotated ([user-admin.md](user-admin.md#rotating-the-mfa-key)).

## Verification: three levels

| Level | When | Who | What it proves |
| --- | --- | --- | --- |
| 1 | after every dump, automatic | the backup service | `pg_restore --list` reads the dump, restic's snapshot is written, the heartbeat is pinged. A failed run pings `…/fail`. |
| 2 | every Sunday 03:30 UTC, automatic | the backup service | `restore --verify`: the newest dump restores into a private scratch PostgreSQL inside the backup container, and every row count, the migration head, the newest audit event and the attachment list equal the manifest; every attachment in the dump's `.media/` matches its SHA-256. |
| 3 | **every quarter**, by hand | the maintainer, on an isolated machine | The whole recovery from the offsite copy only. See [the drill](#quarterly-offsite-drill). |

Level 2 by hand, any time:

```bash
dc exec backup restore --verify            # the newest dump
dc exec backup restore --verify gmat-<stamp>.dump     # a named one
dc exec backup restore --list              # the dumps, newest first
```

Check on level 1 and 2 monthly, even though the heartbeat alerts you: `ls -l backups/.last-success backups/.last-verify-ok` (their modification times are the last good run) and `dc logs --since 24h backup`.

A backup that has not been restored is a hope, not a backup. If level 2 fails, treat it as an incident ([incident.md](incident.md)) the same day.

## Taking a backup by hand

Before risky work (a manual data fix, a large import):

```bash
dc exec backup backup-now --label before-import
```

It makes the dump, manifest and media copy and uploads them offsite. Use a short label (letters, digits and dashes).

## Quarterly offsite drill

The drill proves that the offsite copy, the offline keys and the written procedure are enough to rebuild the service **without the VPS**. It restores into containers whose networks are all internal, with `OUTBOUND_EMAIL=off`, and then wipes everything: the data contains personal data.

### The machine

An isolated machine you control, with an **encrypted disk**, Docker with the Compose plugin, `python3`, a checkout of this repository, and **no access to production** (no VPN, no SSH keys to the VPS, no production env files). A laptop is fine. The drill's directory must never have run a deploy (`restore-drill.sh` refuses a directory that has a `deployed.log`).

You need from the offline storage: `.env` and `app.env` (or at least the database role settings and `RESTIC_*`) and the **delete-capable or read credentials** for the restic repository. Reading a snapshot needs no delete right: use the append-only credentials if you prefer.

### Run it

The drill stack starts neither the scanner nor the decoder: it stores no upload, so an upload there is refused, and neither would have its updates anyway with every network internal.

1. Pick the release whose images production runs now (`tail -1 deployed.log` on the VPS gives version and both digests; or the release's `release-manifest.json`). Pull both images **by digest** while you still have a network:

   ```bash
   docker pull ghcr.io/<owner>/gmat-m8@sha256:<app digest>
   docker pull ghcr.io/<owner>/gmat-m8-backup@sha256:<backup digest>
   ```

2. Fetch the snapshot's files while you still have a route to the repository, so the drill can then run with none:

   ```bash
   restic -r <repository> snapshots                     # choose the snapshot id
   mkdir -p ~/drill-input && restic -r <repository> restore <id> --target ~/drill-input
   ls ~/drill-input/backups                             # gmat-<stamp>.dump, .manifest.json, .media/
   ```

   Or let the script fetch it (`--snapshot <id>`, with the `RESTIC_*` values in the environment or `.env`; this one step needs the route).
3. Cut the network (airplane mode, unplug, or a firewall rule), then:

   ```bash
   cd gmat-m8/docker_compose/gmat_m8
   cp /path/to/offline/.env /path/to/offline/app.env . && chmod 600 .env app.env
   ./restore-drill.sh \
     --dump gmat-<stamp> --from-dir ~/drill-input/backups \
     --app-image ghcr.io/<owner>/gmat-m8@sha256:<app digest> \
     --backup-image ghcr.io/<owner>/gmat-m8-backup@sha256:<backup digest> \
     --operator drill-operator@example.org
   ```

   Optionally add `--preserved-dump FILE` (a *later* dump, standing for a failed database) to prove the replay of revocations; without it the drill proves that privileged accounts come up suspended. Do both over the year.
4. Read the output. It prints `PASS`/`FAIL` for each of these and exits 0 only if all passed:
   - the runtime role connects and can write;
   - the audit table refuses `UPDATE` and `DELETE`;
   - row counts, migration head and newest audit event equal the manifest;
   - a sample of attachments opens and matches its checksum;
   - a session cookie, invitation token, reset link, signed form key and recovery codes from before the restore are all rejected;
   - a revocation recorded after the snapshot is re-applied (with `--preserved-dump`), or privileged accounts are suspended (without);
   - queued messages, supplier dispatches included, are `QUARANTINED` and nothing was sent;
   - `web` and the `worker` cannot open an outbound HTTP or SMTP connection.
5. **Wipe.** Add `--wipe` to the command of step 3 (it wipes once the checks have run), or run `docker compose -f docker-compose.yml -f docker-compose.restore-drill.yml -p gmat-m8-restore-drill down --volumes` and delete `backups/gmat-*` and `drill/` yourself. Check afterwards:

   ```bash
   docker ps -a; docker volume ls; ls backups drill 2>/dev/null
   rm -rf ~/drill-input
   ```

6. Add the line the script printed (`| date | dump | digest | verdict |`) to [drill-log.md](drill-log.md), with a note on anything odd, and commit it in a PR.

A failed check is an incident: do not close the drill until the cause is understood and the drill passes again. The scripts themselves are rehearsed against real containers on every change ([../deploy-rehearsal.md](../deploy-rehearsal.md)); a drill failure on real data is more likely a missing key, wrong digest or an incomplete snapshot.

## Restoring

Pick the row that matches the loss. All restores are run with `recover.sh` (so they take the deploy lock, put both gates on and end with the internal verification and the commit point), by a **platform operator** account.

### A failed deploy

[deploy-rollback.md](deploy-rollback.md): `recover.sh restore-predeploy` restores the pre-deploy dump with no loss, since writes were stopped.

### Wrong data after the commit point

An explicit decision, recorded in `incident.log`:

```bash
./recover.sh restore gmat-<stamp> --operator you@example.org --incident "what and why"
```

It states the loss window (from when that dump was taken until now) and asks you to type `RESTORE` before it changes anything; answering anything else leaves the site as it was. Then it preserves the failed state (`preserved/<time>/`).

**If the database does not start or cannot be read**, add `--db-unreadable`. It copies the `pg_data` volume aside (`gmat-m8_pg_data_failed_<time>`), **removes the original**, starts an empty cluster from the role bootstrap in `.env`, and restores into that. Nothing tells it what happened after the dump, so every privileged account comes up suspended, as after the loss of the server: confirm them with `manage confirm_account`.

### The VPS is gone

Time budget 4 hours. You need: the offline copy of `.env` and `app.env`, the append-only (or admin) restic credentials, a GitHub token that can read the repository, and a DNS record you can change.

1. Create the new server and follow [vps-setup.md](vps-setup.md) through step 5 (no `init.sh`). Use the same domain name or update `APP_DOMAIN` and the allowed hosts.
2. Put the offline `.env` and `app.env` in place, mode 0600. Change nothing yet except what the new server requires.
3. Point the DNS record at the new address. Caddy needs it to obtain a certificate; the maintenance page is served from the start.
4. List the snapshots and pick the newest good one (`restic -r … snapshots`; the newest is not always right: if the last run was during the incident, take the one before).
5. Run the restore. It finds the release's digests from the signed manifest of the version you name, which should be the version that ran last (`deployed.log` is lost with the VPS: use the newest GitHub Release you deployed):

   ```bash
   ./recover.sh restore-offsite --snapshot <id> --version X.Y.Z --operator you@example.org \
     --incident "VPS lost, new server <provider>" \
     [--gap-offboarded offboarded.txt] [--gap-credentials-changed changed.txt]
   ```

   The two optional files list one address per line: people offboarded, or whose password or second factor changed, between the snapshot and the loss. The accounts in them are deactivated or lose their credentials. If you do not know yet, leave them out and run `confirm_account` later.
6. With no failed database the status of accounts in the gap cannot be established, so **every privileged account is suspended** (Q23, below) until confirmed.
7. Follow [After any restore](#after-any-restore).

Data written after the snapshot (up to 6 hours) is gone. Tell the people; users can re-enter their last hours' requests and receipts.

## After any restore

`post-restore-reconcile` runs inside every restore, **before** either gate opens, so it is already done when `recover.sh` finishes. Know what it did:

- a new `SECRET_KEY` (no fallback), every session and the whole session list gone, and every session epoch advanced: **everyone has to sign in again**; password-reset links and signed form keys from before are dead. The MFA and outbox keys are unchanged, so TOTP enrollments survive;
- every pending invitation and email change is revoked, and unsent security email is wiped: re-issue them ([user-admin.md](user-admin.md));
- every set of **recovery codes** is deleted: each user generates a new set at their next sign-in; privileged accounts are made to before anything else;
- the **outbox is quarantined**: everything `QUEUED`, `SENDING`, `FAILED` or `AMBIGUOUS` is `QUARANTINED`, unless the preserved failed database recorded a final outcome for it. Nothing is sent until you release it;
- with a preserved failed state, the access-reducing and credential events after the snapshot are **replayed** (revocations, shortened validity, offboardings, deactivations, projects stopped, password changes). What *gave* access is only reported, never replayed: enter it again by hand;
- an **email change** or a **removed second factor** after the snapshot is not applied ([ADR-0016](../adr/0016-restores-hold-what-can-hand-over-an-account.md)): the account comes up suspended until you accept or reject it, because whoever broke in could have written that row of the failed database;
- without one, privileged accounts (`GROUP_ADMIN`, `MATERIAL_MANAGER`, `ENGINEER`, staff, and superusers except the operator) are **suspended**.

Your to-do list, in order:

1. **Read the reconciliation report** (`preserved/<time>/reconcile-report.json`).
2. **Confirm suspended accounts** one by one. Ask the owner whether their password or authenticator changed during the gap. For an account listed as `old -> new (email change)` or `(totp removal)`, ask whether that change is theirs, by a channel other than the account's email. Then:

   ```bash
   dc run --rm --no-deps web manage confirm_account --operator you@example.org --list
   dc run --rm --no-deps web manage confirm_account --operator you@example.org --email person@example.org [--credentials-changed] [--email-change accept|reject] [--factor-removal accept|reject]
   ```

   `--credentials-changed` gives the account an unusable password and removes its authenticators, so it resets through an invitation link from an administrator. `--email-change` is required when a change is listed: `accept` moves the account to the new address, `reject` keeps the restored one. `--factor-removal` is required when a removal is listed: `accept` removes those factors and the person enrolls again, `reject` keeps them. `--credentials-changed` answers it too. Name the account by its restored address.

   A rejection means the row was never real: treat it as a sign of the compromise.

   If the report's `own_email_change` or `own_factor_removal` is set, that was on the account you ran this with. You stay signed in with your old address and factors, so that you can confirm the others, and nothing was applied. If the change was really yours, make it again ([user-admin.md](user-admin.md)); if not, treat it as a sign of the compromise.
3. **Release or discard the held email** ([email.md](email.md#after-a-restore-quarantined-messages)). Supplier dispatches **one by one, after checking with the supplier**.
4. **Re-issue** invitations and email changes that were revoked.
5. **Check the data**: open the dashboard, a recent request, a recent receipt, an attachment. Compare the newest document numbers with what people remember.
6. Record the incident ([incident.md](incident.md)) and delete `preserved/<time>` when it is closed.

Django's `changepassword` is disabled (it writes no audit event, so a later restore could not replay it): to fix an account, use *Forgot password* or the invitation flow.

## Offsite retention and pruning (maintainer's machine only)

The VPS's credentials can only add. Old snapshots are removed from the **maintainer's machine**, with the delete-capable credentials that never touch the VPS ([../../docker_compose/offsite/README.md](../../docker_compose/offsite/README.md)). Do it every month or two, so the repository does not grow without bound:

```bash
# REST option: an SSH tunnel to the admin server first
ssh -L 8001:127.0.0.1:8001 backup-host &
export RESTIC_REPOSITORY=rest:http://127.0.0.1:8001/gmat/gmat
export RESTIC_PASSWORD=...  RESTIC_REST_USERNAME=gmat  RESTIC_REST_PASSWORD=...   # the ADMIN password
python scripts/offsite_prune.py --dry-run
python scripts/offsite_prune.py
```

The **first time**, and if you ever lose its record, it has nothing to compare with: it lists the snapshots and asks you to read them and run once with `--baseline`. After each prune it remembers what it saw in `~/.config/gmat-m8/offsite-prune.json`, on your machine; back that file up with your other offline material, and run the prune from the same machine.

(`scripts/offsite_prune.py` needs `restic` and only Python's standard library.) It keeps every snapshot of the last 90 days plus 4 weekly and 12 monthly ones.

The script **refuses to run** when any snapshot has an unexpected host or tag, a timestamp in the future, an abnormal count per day, or is new since the last prune but dated before it. That is the sign of forged snapshots appended from a compromised VPS, which could push real old snapshots out of the weekly and monthly rules. Never prune around it: treat it as a security incident ([incident.md](incident.md)), keep the repository as it is, and investigate the VPS.

After a prune it lists the repository again, because restic exits 0 even when a server refuses a removal. To confirm that the VPS's credentials cannot delete, look for the refusal and the listing, never at the exit code.

## Facts to keep handy

| Question | Answer |
| --- | --- |
| Where are the logs of the backup service? | `dc logs backup` (supercronic prints every run) |
| How big is a dump? | The manifest next to it lists row counts and sizes |
| Lost the restic password? | Offsite backups are unreadable, permanently. Keep the offline copy ([vps-setup.md](vps-setup.md#7-keep-the-keys-somewhere-else)) |
| Lost the MFA or outbox key? | [user-admin.md](user-admin.md#encryption-keys-mfa-and-outbox) |
| Where are attachments in a backup? | `gmat-<stamp>.media/`, restored together with the dump |
