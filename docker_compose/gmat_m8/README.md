# gmat-m8 compose stack

The full stack, ready to use. Local by default: `https://localhost:8443`, Caddy's internal CA, images built from this checkout.

```text
                      internet / browser
                              │  80/443 (8080/8443 locally)
                        ┌─────▼─────┐
                        │   caddy   │  TLS, limits, maintenance gate, redacted access log
                        └─────┬─────┘
                              │ edge (internal; only caddy and web)
                        ┌─────▼─────┐
                        │    web    │  Gunicorn + Django
                        └─────┬─────┘
          db_web              │              db_migrate     db_backup
   ┌──────────────────────────▼──────────────────────────────────────┐
   │ db (PostgreSQL 18)  — joined by one internal network per client │
   └───────▲──────────────────────────▲──────────────────────▲──────┘
           │ migrate (one-shot)       │ backup (cron)        │ worker (outbox, db_worker)
                                      └───── egress ─────────┴──► SMTP relay / offsite storage
                                                                  (Mailpit on a local stack)
```

| Service | Image | Networks | Notes |
| --- | --- | --- | --- |
| `caddy` | `caddy:2.11.9-alpine` (digest) | `ingress`, `edge` | The only service that publishes ports. Admin API off. uid 1000, listening on 8080 and 8443; production publishes 80 and 443 onto them. |
| `caddy-perms` | `caddy:2.11.9-alpine` (digest) | none | One-shot before Caddy starts: gives Caddy's two volumes to uid 1000, then exits. The only container that runs as root, with `CHOWN` and `DAC_READ_SEARCH` and nothing else to do. |
| `migrate` | `gmat-m8` | `db_migrate` | One-shot `migrate` as the **owner** role; `web` waits for it. |
| `web` | `gmat-m8` | `edge`, `db_web` | Runtime role (data access only). No route to the internet. |
| `db` | `postgres:18-alpine` (digest) | `db_web`, `db_worker`, `db_backup`, `db_migrate` | Never published (except in the dev overlay, on 127.0.0.1). |
| `worker` | `gmat-m8` | `db_worker`, `egress` | The only sender of email: claims outbox rows, delivers them through SMTP, runs the hourly housekeeping. Runtime role. Idle while `flags/worker.paused` exists; never sends with `OUTBOUND_EMAIL=off`. Heartbeat healthcheck. |
| `backup` | `gmat-m8-backup` | `db_backup`, `egress` | Dumps every 6 hours; `backup-now`, `restore --verify`. |
| `decoder` | `gmat-m8` | none | Decodes uploaded images (ADR-0015): uid 1001, no secret in its environment, reached by `web` through a socket on its own tmpfs volume. |
| `clamd` | `clamav/clamav` (digest; x86-64) | none | Scans every upload before it is stored (ADR-0015), reached the same way. About 1 GB of memory, a minute to start. |
| `freshclam` | `clamav/clamav` (digest; x86-64) | `updates` | Keeps the signature volume current. Sees no uploaded file. |
| `mailpit` | `axllent/mailpit` (digest) | `egress` | Local and CI stacks only (`docker-compose.local-mail.yml`, loaded through `COMPOSE_FILE` in `.env`): the worker delivers here, nothing leaves the host. |

Every container drops all capabilities, sets `no-new-privileges` and has a read-only root filesystem. `web`, `worker`, `migrate`, `backup` and Mailpit run as uid 1000, `decoder` as uid 1001, `clamd` and `freshclam` as their image's `clamav` user (uid 100), `db` as its image's `postgres` user (uid 70), and Caddy as uid 1000 with no capability. Nothing that keeps running is root. `caddy-perms` runs as root for one `chown` before Caddy starts (compose's `depends_on`; `deploy.sh` and `recover.sh` run it themselves), because a volume written by an older release belongs to root. To run a release older than v0.4.0 from its own checkout, give the volumes back first ([docs/runbooks/deploy-rollback.md](../../docs/runbooks/deploy-rollback.md#a-release-before-v040)).

## First start

```bash
cd docker_compose/gmat_m8
bash init.sh                                  # .env and app.env with generated secrets (mode 0600)
docker compose up -d --build --wait           # https://localhost:8443 (accept or trust Caddy's CA)
```

`init.sh` never overwrites an existing env file and prints no secret. Run it again after an upgrade: it appends to existing env files only the keys a newer template added (generating secrets such as `OUTBOX_ENCRYPTION_KEYS`) and prints their names. Flags: `--ci` (for a CI runner on a root daemon; refused on a rootless one), `--reset-db [--yes]` (deletes the database volume after asking).

The first administrator is invited from a shell, while no active group administrator exists (a second run is refused once one does):

```bash
docker compose run --rm web manage bootstrap_admin --email you@example.com --full-name "Your Name" --company-code A [--company-name "Company A"] [--superuser]
```

It creates the company and its head office if they are missing and prints a one-time setup link (valid 72 hours, shown only in this terminal). Open it at `https://localhost:8443` to set the password; the first sign-in then enrolls MFA. Running it again before the link is used replaces the link. `createsuperuser` is disabled: `--superuser` also makes the account a platform operator. Everyone else is invited from the app (People → Invite someone).

## Configuration

| File | Holds |
| --- | --- |
| `.env` | Compose-level values: domain, ports, subnet, image names, database roles and passwords, the SMTP password (`SMTP_PASSWORD`, passed to the worker only), backup and restic settings, and `COMPOSE_FILE` (adds Mailpit to a stack started without `-f`). Each secret is defined **once** here and reaches the services by interpolation. |
| `app.env` | Django-only settings (secret key, MFA and outbox keys, allowed hosts, session ages, `OUTBOUND_EMAIL` and the SMTP relay). `app.env.production.example` is the production template. |
| `caddy/Caddyfile` | Site configuration; `caddy/maintenance/index.html` is the maintenance and fallback page; `caddy/edge/` holds one file per value of each edge mode (below). |
| `tls/`, `tunnel/` | The edge's files, only with its modes: the certificate and key Caddy serves (`EDGE_TLS=files`), the tunnel's token and the CA pool its connector checks Caddy against (`EDGE_PROXY=cloudflare`). Created empty by `init.sh`; each key or token is given to the containers' uid with mode 0400 by `init.sh` once it is in place. Never committed. |
| `flags/` | `maintenance.on` closes the public site (503 page) while `web` keeps running; `worker.paused` stops the worker from claiming, sending and cleaning up (it keeps its heartbeat). Read-only in the containers; written only by deploy and recovery tooling. |
| `backups/` | Local dumps (mode 0700, files 0600, kept 3 days). |
| volume `media` | Attachments (M6.4): `files/` (stored, random names) and `staging/` (uploads in progress). `web` and the worker mount it read-write, the backup read-only (`MEDIA_DIR`); Caddy never sees it. Quota `ATTACHMENT_STORAGE_LIMIT_MB` in `app.env` (default 5120). |

None of the env files is committed; only the `*.example` templates are.

### The edge (optional)

All off by default: without these `.env` keys the site is served as above. `bash init.sh` and `deploy.sh` check them (`ops/checks.py edge`) and refuse any other spelling; run `init.sh` again after changing one.

| Key | Values | What it does |
| --- | --- | --- |
| `EDGE_TLS` | `acme` (default), `files` | `files`: Caddy serves `tls/cert.pem` and `tls/key.pem`, asks ACME for nothing, and gives that certificate to a client that names no host (a browser on an IP address) |
| `EDGE_EXTRA_HOST` | empty, or one host name | one more name the site answers (the tunnel's); add it to `DJANGO_ALLOWED_HOSTS` |
| `EDGE_PROXY` | `none` (default), `cloudflare` | `cloudflare`: visitors come through Cloudflare Tunnel, whose connector runs as the `cloudflared` service (with `COMPOSE_PROFILES=tunnel`); Caddy believes their address from that connector alone. Needs `EDGE_TLS=files` |
| `EDGE_TUNNEL_SUBNET`, `EDGE_TUNNEL_RANGE`, `EDGE_TUNNEL_ADDRESS` | `172.30.90.0/24`, `172.30.90.0/25`, `172.30.90.250` | the `tunnel` network that only Caddy and the connector join, the range Caddy takes its address from, and the connector's fixed address outside it |

The links in email use the first `DJANGO_CSRF_TRUSTED_ORIGINS` entry of `app.env`, which the check prints. The design and its risks: `docs/adr/0034-edge-modes-opt-in.md` and `docs/security.md` (*The edge*).

## Overlays

| Command | Purpose |
| --- | --- |
| `docker compose up -d --build --wait` (no `-f`) | The local stack: `COMPOSE_FILE` in `.env` adds `docker-compose.local-mail.yml` (Mailpit). |
| `docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build` | Development: `dev` image target, source bind-mounted read-only, `runserver`, the worker, Tailwind in watch mode, Mailpit on 127.0.0.1:8025, PostgreSQL on 127.0.0.1:5432. |
| `./deploy.sh X.Y.Z` (`docker-compose.production.yml`) | Production on the VPS: ports 80/443 (tcp+udp), ACME for `APP_DOMAIN`, both images **by digest** from GHCR (`APP_DIGEST`, `BACKUP_DIGEST`; the worker runs the app image), no build, no Mailpit (mail goes to `EMAIL_HOST`). See "Deploying and recovering". |
| `./restore-drill.sh ...` (`docker-compose.restore-drill.yml`) | The quarterly restore drill on an isolated machine: own project name and volumes, **every network internal**, nothing published, `OUTBOUND_EMAIL=off`. |

Seed data for development (the dev overlay only; the command refuses any settings but `config.settings.dev` and `config.settings.test`):

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml run --rm web manage bootstrap_admin --email you@example.com --full-name "Your Name" --company-code A   # once: prints the setup link
docker compose -f docker-compose.yml -f docker-compose.dev.yml run --rm web manage seed_demo --operator you@example.com [--password …]
```

`seed_demo` runs as that group administrator and, through the same services as the app, creates the A/B/C organization of plan §3.2 (companies A, B (joint venture of A and C) and C; branches A-MAD, A-SEV and B-MAD, managed by A-MAD; warehouses), one user per role plus a user with mixed memberships (`mm-mad@demo.example`, `mm-sev`, `eng-mad`, `wo-mad`, `tech-mad`, `site-manager`, `partner`, `mixed`), four projects (one on hold; the site manager and the partner are shared on `HOSP-X`), about 200 materials and 5 suppliers. The password of the new users is `--password` or a random one, printed once. Privileged roles enroll MFA at their first sign-in; with Mailpit (`http://127.0.0.1:8025`) password resets work. It is idempotent: a second run reports everything as existing. To load your own data instead, see `docs/import-templates/` (`manage import_catalog | import_suppliers | import_projects <file> --operator <email> [--dry-run]`).

## Local mail (Mailpit)

On a local stack (no `-f`, or the dev overlay) every email goes to Mailpit, never to a real inbox: an invitation, an email change or a password reset sent to `someone@example.org` stays on your machine. The worker delivers (the admin screens show "queued", then "sent" once it has; see "Delivery status" in the app), and Mailpit keeps the message until its container is recreated.

Mailpit is not published by the local-mail overlay, so read it from inside the network:

```bash
# list the messages (JSON: recipient, subject, time)
docker compose exec mailpit wget -qO- http://localhost:8025/api/v1/messages
# the newest one, with its text (the one-time link of an invitation or reset is in "Text")
docker compose exec mailpit wget -qO- http://localhost:8025/api/v1/message/latest
```

Pipe either through `python3 -m json.tool` (or `jq`) to read it. The dev overlay publishes the web UI on `http://127.0.0.1:8025` instead. If a message never arrives, check that the worker is running and not paused (`flags/worker.paused`) and that `OUTBOUND_EMAIL` in `app.env` is not `off`: the app waits for the worker and shows a message still queued after ten minutes as delayed.

## Day to day

```bash
docker compose logs -f caddy web worker                # Caddy is the only HTTP access log
docker compose exec worker /app/docker/app/entrypoint.sh worker-healthcheck   # heartbeat fresh?
touch flags/worker.paused                              # worker claims and sends nothing; rm to resume
docker compose run --rm --no-deps web check            # manage.py check --deploy, zero warnings
docker compose run --rm --no-deps web migrate-plan     # pending migrations, read only
docker compose exec backup backup-now --label manual   # dump + manifest (+ offsite upload)
docker compose exec backup restore --verify            # newest dump vs. its manifest, in a scratch database
touch flags/maintenance.on                             # public site answers 503; rm flags/maintenance.on to reopen
```

`restore --verify` starts a private PostgreSQL inside the backup container (unix socket, no network), restores the dump there and compares every row count, the migration head, the newest audit event and the attachment list with the manifest, then checks every listed attachment in the dump's `.media/` copy against its SHA-256. Sessions and the rate-limit cache are never part of a dump. The backup copies the attachments while it holds `gmat:media-backup`; the worker deletes removed files only while it can take that lock, and only a day after their rows went (plan §9).

## Deploying and recovering

Production only, on the VPS, by the maintainer; each script's header has the details, and the step-by-step runbooks are in [`docs/runbooks/`](../../docs/runbooks/README.md). Needs `docker`, `curl`, `flock`, `python3` and `cosign` on the host, a `.env` and `app.env` of mode 0600, an offsite repository (`../offsite/README.md`), and, only if `DJANGO_ADMIN_ENABLED` turns the admin on, `ADMIN_ALLOWED_CIDRS` set.

```bash
git fetch --tags && git switch --detach vX.Y.Z   # the release's own compose files
./deploy.sh X.Y.Z                                # verifies, gates, backs up, migrates, verifies, opens
./recover.sh status                              # where it stopped, and the matching row below
```

| Situation | Command | Data loss |
| --- | --- | --- |
| Failed before step 7 | `./recover.sh rollback` | None |
| Failed at steps 7-9, a reversible release | `./recover.sh migrate-back` | None |
| Failed at steps 7-9, otherwise | `./recover.sh restore-predeploy --operator EMAIL` | None |
| A defect after the commit point | Forward-fix first (`./deploy.sh X.Y.Z+1`). `./recover.sh rollback-images` only if the signed manifest says `Old code works on the new schema` | None |
| Only a restore can fix it, after writes were accepted | `./recover.sh restore DUMP --operator EMAIL --incident TEXT`: an explicit decision. It keeps the failed state and states the loss window first | Everything since the deploy, unless re-entered |
| The VPS is gone | On the new one: `./recover.sh restore-offsite --snapshot ID --version V --operator EMAIL --incident TEXT` | Up to the RPO (6 h) |

Every action but `status` takes the deploy lock and both gates, and ends with the internal verification and the commit point. A failure leaves both gates on: the public site answers 503 and the worker sends nothing. `EMAIL` is a platform operator (`bootstrap_admin --superuser`).

After a restore:

```bash
docker compose -f docker-compose.yml -f docker-compose.production.yml run --rm --no-deps web manage outbox_quarantine --operator EMAIL list
# notifications in bulk: release-notifications | discard-notifications
# supplier dispatches one by one, after checking with the supplier: confirm-dispatch | release-dispatch | discard-dispatch ID
docker compose -f docker-compose.yml -f docker-compose.production.yml run --rm --no-deps web manage confirm_account --operator EMAIL --list
# each account once its owner confirmed it: --email ADDRESS [--credentials-changed] [--email-change accept|reject] [--factor-removal accept|reject]
```

Files the tooling keeps here, all git-ignored: `deployed.log` (one line per deploy or recovery: time, version, both digests, dump, kind), `deploy.state` and `deploy.head` (the deploy in progress), `.release/` (verified manifests and notes), `preserved/` (the failed states, with personal data: delete them when the incident is closed), `incident.log`, and the renamed `pg_data` volumes (`docker volume ls`).

The drill (`./restore-drill.sh`, see its header) never runs next to production: it refuses a directory that has a `deployed.log`.

## Security notes

- **Networks.** `web`, `migrate`, `backup` and `worker` each reach the database through their own internal network, so none of them can reach another (the live suite proves the worker cannot reach `web`). The worker and the backup reach the outside through `egress`. `edge` is internal and shared only by Caddy and `web`; Gunicorn trusts forwarded headers from that subnet only (`EDGE_SUBNET`, pinned). `web` has no route to the internet.
- **Client address.** Caddy trusts no client-sent `X-Forwarded-*` header, so rate limits see the address Caddy observed. With `EDGE_PROXY=cloudflare` it believes `CF-Connecting-IP` from the tunnel's connector alone, at its one fixed address, and still writes the `X-Forwarded-For` Django reads itself.
- **Logs.** Caddy's filter replaces token paths (`/accounts/password/reset/key/…`, and the invitation and email-change routes from M2.6) and every query string with `REDACTED`, on every host name, and drops `Referer`, `HX-Current-URL` and Cloudflare Access's `Cf-Access-Jwt-Assertion`. Gunicorn's access log is off; its error log goes through the same redaction as Django's.
- **Admin.** Everything is administered in the app; the Django admin is optional and read-only, off unless `DJANGO_ADMIN_ENABLED=true` in `.env` (compose gives the same switch to Caddy and Django). Off, its prefix is an unknown path (404). On, `ADMIN_URL_PATH` sets the prefix for both and `ADMIN_ALLOWED_CIDRS` restricts it at Caddy (everyone else gets a 404): a local stack defaults to open, in production an empty list lets nobody in and `deploy.sh` refuses an empty or open one. The admin also needs an MFA-verified session.
- **Database roles.** `owner` (migrations only), `runtime` (DML only, so it cannot alter or drop tables), `backup` (`pg_read_all_data`, read-only sessions). Passwords are generated by `init.sh` and refused if weak.
- **Mail.** Requests never talk to SMTP: they write `EmailOutbox` rows and the worker sends them. Sensitive bodies (reset links) are encrypted with `OUTBOX_ENCRYPTION_KEYS` and wiped once sent or after an hour. The SMTP password reaches the worker only.
- **Secrets.** The restic repository for offsite backups must be **append-only** from this host; `forget`/`prune` run only from the maintainer's machine with other credentials (`../offsite/README.md`, `scripts/offsite_prune.py`).
- **Uploads.** Caddy refuses bodies above `MAX_UPLOAD_SIZE` (11 MiB) and cuts a body that stalls for `CADDY_READ_BODY_TIMEOUT` (120 s); every file is scanned by `clamd`, and with no answer it is refused (503); images are decoded in the `decoder` container by a child with memory and CPU limits, killed after 60 s (the development overlay starts neither unless asked, `--profile uploads`, and its settings scan nothing and decode in a child of `web`). Files are downloaded only through the app, as attachments with a sandbox CSP.
- **Docker and ufw.** On the production host, which runs Docker rootless, a published port is a process listening on the host and ufw's INPUT rules apply to it: allow only 22/tcp, 80/tcp, 443/tcp and 443/udp (`docs/runbooks/vps-setup.md` §2). On a root daemon (CI, a developer's machine), Docker's own rules go ahead of ufw's and bypass it. Either way only Caddy publishes anything; keep it that way.
- **Rootless host.** No `docker` group, no root daemon: the daemon is `deploy`'s own, the port driver is `slirp4netns` (the visitor's address reaches Caddy), and `backups/` belongs to the containers' shifted uid, reached by the scripts through one checked helper. `deploy.sh` and `recover.sh status` check the host; the setup is `docs/runbooks/vps-setup.md` §4, the design `docs/rootless-host.md`.

## Tests

`tests/live` exercises a running stack (`LIVE_REQUIRED=1 uv run pytest -m live`, see `tests/live/conftest.py`). The `compose-smoke` workflow runs it on every pull request that touches the stack, and always on `main` and nightly.

`tests/rehearsal` proves `deploy.sh`, `recover.sh`, the restore drill and the offsite rules against real containers in throwaway projects (`REHEARSAL_REQUIRED=1 uv run pytest tests/rehearsal -m rehearsal -s`, about 21 minutes, about 3 GB free at its peak; the result of the last run is in `docs/deploy-rehearsal.md`). `tests/unit/docker/test_deploy_scripts.py` runs the same scripts against fakes on every CI run.

`tests/e2e` drives a browser through the same stack (`E2E_REQUIRED=1 uv run pytest tests/e2e -m e2e`, see `tests/e2e/CLAUDE.md` for the one-time setup). It needs an **empty database**: it creates the first administrator through `manage bootstrap_admin` and the browser, then runs `seed_demo` in a one-off `web` container with the development settings (the stack keeps serving with the production ones), so start it fresh (`docker compose down --volumes && docker compose up -d --build --wait`; give a stack started with `-p <name>` as `E2E_COMPOSE_PROJECT`). The `e2e` workflow runs it on `main`, nightly, by hand and on a pull request labelled `e2e`.
