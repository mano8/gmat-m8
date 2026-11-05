# Incident response

What to do when something is wrong in production: the site is down, data is wrong, an account or a server may be compromised, or a backup check failed. This page is deliberately short enough to read while it is happening. Details live in the runbooks it points to.

Order of work, always: **stop the damage → keep the evidence → fix → tell people → write it down.** Do not rush a restore: it is the one step that cannot be taken back ([deploy-rollback.md](deploy-rollback.md)).

```bash
cd ~/gmat-m8/docker_compose/gmat_m8
alias dc='docker compose -f docker-compose.yml -f docker-compose.production.yml'
```

## 1. First ten minutes

1. **Write the time** and what you saw, in a text file as you go (`incident-notes.txt` outside the repository). Every later step goes in it.
2. Look before touching:

   ```bash
   ./recover.sh status                        # gates, last deploy, running release
   dc ps                                      # what is up, healthy, restarting
   dc logs --since 30m caddy web worker db    # the last half hour
   df -h /                                    # a full disk explains many things
   ```

3. Classify it (next section) and take the **containment** step for that class. If you cannot tell, treat it as the more serious class.

## 2. What kind of incident

| Looks like | Class | Containment |
| --- | --- | --- |
| The site is down or slow, nobody has done anything strange | **Outage** | Nothing to contain; go to [deploy-rollback.md](deploy-rollback.md#emergency-the-site-is-down-and-no-deploy-is-running) |
| A deploy failed | **Failed deploy** | The script already holds both gates; follow [the decision tree](deploy-rollback.md#the-decision-tree) |
| Wrong or missing business data | **Data problem** | Find out how far it goes before anyone fixes by hand; pause nothing yet. Take `backup-now --label before-fix`. Use the audit viewer (`/audit/`, object history) to see who did what |
| A backup, verification or drill check failed | **Backup failure** | Same day. Run `dc exec backup backup-now --label manual` and `dc exec backup restore --verify`. Read `dc logs backup`. Check disk space and the offsite credentials |
| A person's account may be misused, a lost device, or a notice about a change they did not make | **Account** | [Offboard](user-admin.md#offboarding) or [reset MFA](user-admin.md#lost-second-factor) now; it ends their sessions. Then read what the account did in the audit viewer |
| Unexpected admin or SSH logins, unknown processes, strange containers, forged offsite snapshots, a key or token leaked | **Compromise** | Go to [section 4](#4-suspected-compromise) |
| Uploads refused with "the virus scanner found something" | **Malware upload** | The file was not stored. Find who and where: the audit viewer's `attachment.malware_refused` on the document, and `attachment_malware` in `gmat.security` (the signature, the account). Ask the person: a phone with malware, or a false alarm. A false alarm is reported to ClamAV; there is no way to let one file past the scanner |
| Mail went to the wrong people or twice | **Email** | `touch flags/worker.paused` to stop sending while you look, then [email.md](email.md) |

Gates, when you need them: `touch flags/maintenance.on` makes Caddy answer the maintenance page (503) whatever `web` is doing; `touch flags/worker.paused` stops the worker from claiming or sending. **Remove them yourself only if no deploy or recovery owns them**: `./recover.sh status` says.

## 3. Evidence

Collect before you rebuild or restore; a restore changes the thing you want to examine.

| Evidence | Where |
| --- | --- |
| Security events (sign-ins, failures, factor changes, resets, offboardings) | `dc logs web \| grep gmat.security`. Account ids and short hints, never passwords or the address typed |
| Business history | the audit viewer `/audit/` (append-only, enforced by a database trigger) and an object's history `/audit/object/<type>/<id>/` |
| HTTP access log | `dc logs caddy`. Token URLs and query strings are redacted by design |
| The state of the database | `dc exec backup backup-now --label incident` before touching anything, and keep the dump |
| Worker activity | `dc logs worker`; every dispatch attempt is audited on its order |
| The host | `last -20`, `journalctl -u ssh --since "2 days ago"`, `docker events --since 24h --until now`, `ps aux` |

Do not copy these into chat tools or tickets in full: they hold account ids and times. Share the part that is needed.

## 4. Suspected compromise

Assume the worst until shown otherwise.

1. **Contain.** `touch flags/maintenance.on flags/worker.paused`. The public site shows the maintenance page and nothing is sent.
2. **Keep evidence** (section 3). If an attacker may have root on the VPS, do not trust anything printed *by* that machine; copy the dump and logs off it first.
3. **Decide scope.**
   - *An application account* (phished password, stolen phone): offboard it and review its audit trail. Done; the rest is routine.
   - *The server* (SSH key stolen, container escape, unknown process): **rebuild**. Provision a new VPS ([vps-setup.md](vps-setup.md)), restore from the last good offsite snapshot ([backup-restore.md](backup-restore.md#the-vps-is-gone)), after rotating everything in the table below. The old server is destroyed once you have taken what you need from it.
   - *Offsite storage or its credentials* (see forged snapshots below).
4. **Rotate.** Anything that was on the machine, or that the attacker could read, is burnt:

| Secret | How |
| --- | --- |
| SSH keys of `deploy` | new key pair, remove the old from `authorized_keys` (or on the new server only) |
| `DJANGO_SECRET_KEY` | new value in `app.env`; ends every session and signed link. A restore does this by itself |
| Database role passwords and `POSTGRES_PASSWORD` | change the roles in PostgreSQL (`ALTER ROLE …`) and `.env` together, then recreate the services. On a rebuilt server, initialise with new values |
| `GITHUB_READ_TOKEN` | revoke at GitHub, create a new fine-grained one (this repository only) |
| `SMTP_PASSWORD` | change at the relay |
| The VPS's append-only offsite credentials | revoke and re-issue at the storage provider |
| `RESTIC_PASSWORD` | `restic key passwd` (or `key add` then `key remove` of the old one), **from the maintainer's machine** with the admin credentials; update the offline copy and the VPS's `.env` |
| `MFA_ENCRYPTION_KEYS` | new key, `manage reencrypt_mfa`, and a leak needs resets for everybody: [user-admin.md](user-admin.md#rotating-the-mfa-key) |
| `OUTBOX_ENCRYPTION_KEYS` | [user-admin.md](user-admin.md#rotating-the-outbox-key-routine-or-after-a-suspected-leak) |
| People's passwords | after a restore, accounts that matter come up suspended or reset; after a database leak, require resets by offboarding and re-inviting the privileged accounts |

5. **Forged offsite snapshots.** If `scripts/offsite_prune.py` refused ("unexpected host or tag", "timestamp in the future", "abnormal count per day"), do **not** prune and do not delete by hand to make it pass. Keep the repository, list the snapshots (`restic snapshots`), compare them with `deployed.log` and the backup schedule, treat the VPS as compromised and rebuild. Restore only from a snapshot you can tie to a real backup run.
6. **Redeploy** only from a release whose signatures `deploy.sh` verifies, never from images built or pulled by hand.

## 5. Telling people

- **Users**: say what happened to *their* work in plain words: what is unavailable, what to re-enter, when it is expected back. A restore loses up to 6 hours (or everything since the deploy for a restore-after-writes): name the window.
- **Suppliers**: if purchase orders were affected (lost, duplicated, or a cancellation notice missing), call the suppliers concerned before anyone acts on a stale order ([email.md](email.md#supplier-dispatches)).
- **Personal data.** If personal data may have been exposed to someone it should not have, you may have a legal duty to notify the data protection authority (in the EU within 72 hours of becoming aware) and the people affected. Involve whoever is responsible for data protection in your organization the same day. This is a legal question, not a technical one.
- **Security reporting.** If the cause is a defect in gmat-m8 itself, follow `SECURITY.md` at the repository root.

## 6. Closing

1. Check the service end to end: sign in as a few roles, create and read back a request, a receipt and an attachment; `./recover.sh status` clean; the gates removed by whoever owns them; backups and the verification passing.
2. Delete what the incident created that holds personal data: `preserved/<time>/` and renamed `pg_data` volumes (`docker volume ls`) once they are no longer needed, and any dump copied off the VPS.
3. Write the record below, at the end of `incident.log` (kept next to `deployed.log`, git-ignored) and, if it changes how we work, as a PR to the runbooks or the exception register in `docs/security.md`.

```text
Incident:   <short title>                     Date: <UTC>      Owner: <name>
Detected:   <how, when>
Class:      outage | failed deploy | data | backup | account | compromise | email
Impact:     <who, what, how long; data lost (window)>
Cause:      <what actually happened>
Actions:    <time-ordered, with who ran what>
Restore:    <none | dump id, loss window, decision recorded in incident.log>
Rotated:    <secrets>
Follow-up:  <fixes, runbook changes, owners, dates>
```

`recover.sh restore` and `restore-offsite` already append their decision to `incident.log` with time, dump, loss window, operator and your `--incident` text; the record above adds the rest.
