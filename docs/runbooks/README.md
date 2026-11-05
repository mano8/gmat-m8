# Operations runbooks

Written for the maintainer, who runs the production VPS alone. Each page can be followed from top to bottom without other documents; they link to each other and to the plan's reasoning where it helps. The tooling they use is in `docker_compose/gmat_m8/` (`deploy.sh`, `recover.sh`, `restore-drill.sh`, `ops/`) and `docker_compose/offsite/`; the threat model is in [../security.md](../security.md); the release pipeline in [../release.md](../release.md); the rehearsal that proves the scripts in [../deploy-rehearsal.md](../deploy-rehearsal.md).

| I need to … | Read |
| --- | --- |
| Set up the server for the first time (H8.6) | [vps-setup.md](vps-setup.md) |
| Put a release on the server, or undo a bad one | [deploy-rollback.md](deploy-rollback.md) |
| Check backups, run the quarterly drill, restore, prune offsite | [backup-restore.md](backup-restore.md) |
| Record the result of a drill | [drill-log.md](drill-log.md) |
| Invite or offboard someone, reset a lost second factor, rotate a key | [user-admin.md](user-admin.md) |
| Find out why mail is not leaving, resolve an uncertain supplier mail, release held mail | [email.md](email.md) |
| Something is wrong right now | [incident.md](incident.md) |
| Handle a vulnerability finding or renew an exception | [vulnerability-exceptions.md](vulnerability-exceptions.md) |

## Conventions

- Commands run on the VPS as `deploy`, in `~/gmat-m8/docker_compose/gmat_m8`. `dc` is `docker compose -f docker-compose.yml -f docker-compose.production.yml`.
- `EMAIL` / `you@example.org` is a **platform operator**: an account created with `manage bootstrap_admin --superuser`. Management commands that take `--operator` run as that person and are audited under their name.
- A human decides and a script executes. Agents never run deploys, restores or anything on the VPS.
- Secrets never go into a ticket, chat or commit. The env files are mode 0600 and only the `*.example` templates are in the repository.

## Calendar

| How often | What |
| --- | --- |
| Every deploy | [deploy-rollback.md](deploy-rollback.md), outside working hours |
| Weekly, automatic | `restore --verify`; look at `backups/.last-verify-ok` monthly |
| Every month or two | prune offsite from the maintainer's machine |
| **Every quarter** | the offsite restore drill, then a line in [drill-log.md](drill-log.md) |
| **At most every 90 days** | review `.trivyignore.yaml` and the exception register ([vulnerability-exceptions.md](vulnerability-exceptions.md)) |
| Before they expire | the GitHub read-only token, the SMTP and offsite credentials |
