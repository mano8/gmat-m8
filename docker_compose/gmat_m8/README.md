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
| `caddy` | `caddy:2.11.7-alpine` (digest) | `ingress`, `edge` | The only service that publishes ports. Admin API off. |
| `migrate` | `gmat-m8` | `db_migrate` | One-shot `migrate` as the **owner** role; `web` waits for it. |
| `web` | `gmat-m8` | `edge`, `db_web` | Runtime role (data access only). No route to the internet. |
| `db` | `postgres:18-alpine` (digest) | `db_web`, `db_worker`, `db_backup`, `db_migrate` | Never published (except in the dev overlay, on 127.0.0.1). |
| `worker` | `gmat-m8` | `db_worker`, `egress` | The only sender of email: claims outbox rows, delivers them through SMTP, runs the hourly housekeeping. Runtime role. Idle while `flags/worker.paused` exists; never sends with `OUTBOUND_EMAIL=off`. Heartbeat healthcheck. |
| `backup` | `gmat-m8-backup` | `db_backup`, `egress` | Dumps every 6 hours; `backup-now`, `restore --verify`. |
| `mailpit` | `axllent/mailpit` (digest) | `egress` | Local and CI stacks only (`docker-compose.local-mail.yml`, loaded through `COMPOSE_FILE` in `.env`): the worker delivers here, nothing leaves the host. |

Every container drops all capabilities, sets `no-new-privileges` and has a read-only root filesystem. `web`, `worker`, `migrate`, `backup` and Mailpit run as uid 1000; Caddy runs as root inside its container with only `NET_BIND_SERVICE` (its data volumes are root-owned).

## First start

```bash
cd docker_compose/gmat_m8
bash init.sh                                  # .env and app.env with generated secrets (mode 0600)
docker compose up -d --build --wait           # https://localhost:8443 (accept or trust Caddy's CA)
```

`init.sh` never overwrites an existing env file and prints no secret. Run it again after an upgrade: it appends to existing env files only the keys a newer template added (generating secrets such as `OUTBOX_ENCRYPTION_KEYS`) and prints their names. Flags: `--ci` (for a CI runner), `--reset-db [--yes]` (deletes the database volume after asking).

The first administrator is invited from a shell, while no active group administrator exists (a second run is refused once one does):

```bash
docker compose run --rm web manage bootstrap_admin --email you@example.com --company-code A [--company-name "Company A"] [--superuser]
```

It creates the company and its head office if they are missing and prints a one-time setup link (valid 72 hours, shown only in this terminal). Open it at `https://localhost:8443` to set the password; the first sign-in then enrolls MFA. Running it again before the link is used replaces the link. `createsuperuser` is disabled: `--superuser` also makes the account a platform operator. Everyone else is invited from the app (People → Invite someone).

## Configuration

| File | Holds |
| --- | --- |
| `.env` | Compose-level values: domain, ports, subnet, image names, database roles and passwords, the SMTP password (`SMTP_PASSWORD`, passed to the worker only), backup and restic settings, and `COMPOSE_FILE` (adds Mailpit to a stack started without `-f`). Each secret is defined **once** here and reaches the services by interpolation. |
| `app.env` | Django-only settings (secret key, MFA and outbox keys, allowed hosts, session ages, `OUTBOUND_EMAIL` and the SMTP relay). `app.env.production.example` is the production template. |
| `caddy/Caddyfile` | Site configuration; `caddy/maintenance/index.html` is the maintenance and fallback page. |
| `flags/` | `maintenance.on` closes the public site (503 page) while `web` keeps running; `worker.paused` stops the worker from claiming, sending and cleaning up (it keeps its heartbeat). Read-only in the containers; written only by deploy and recovery tooling. |
| `backups/` | Local dumps (mode 0700, files 0600, kept 3 days). |

None of the env files is committed; only the `*.example` templates are.

## Overlays

| Command | Purpose |
| --- | --- |
| `docker compose up -d --build --wait` (no `-f`) | The local stack: `COMPOSE_FILE` in `.env` adds `docker-compose.local-mail.yml` (Mailpit). |
| `docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build` | Development: `dev` image target, source bind-mounted read-only, `runserver`, the worker, Tailwind in watch mode, Mailpit on 127.0.0.1:8025, PostgreSQL on 127.0.0.1:5432. |
| `docker compose -f docker-compose.yml -f docker-compose.production.yml up -d --wait` | Production on the VPS: ports 80/443 (tcp+udp), ACME for `APP_DOMAIN`, both images **by digest** from GHCR (`APP_DIGEST`, `BACKUP_DIGEST`; the worker runs the app image), no build, no Mailpit (mail goes to `EMAIL_HOST`). `deploy.sh` (M8.4) is the supported way to run it. |
| `docker compose -f docker-compose.yml -f docker-compose.restore-drill.yml ...` | The quarterly restore drill on an isolated machine: own project name and volumes, **every network internal**, nothing published, `OUTBOUND_EMAIL=off`. |

Seed data for development: `docker compose -f docker-compose.yml -f docker-compose.dev.yml run --rm web manage seed_demo` (command arrives with M3).

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

`restore --verify` starts a private PostgreSQL inside the backup container (unix socket, no network), restores the dump there and compares every row count, the migration head and the newest audit event with the manifest. Sessions and the rate-limit cache are never part of a dump.

## Security notes

- **Networks.** `web`, `migrate`, `backup` and `worker` each reach the database through their own internal network, so none of them can reach another (the live suite proves the worker cannot reach `web`). The worker and the backup reach the outside through `egress`. `edge` is internal and shared only by Caddy and `web`; Gunicorn trusts forwarded headers from that subnet only (`EDGE_SUBNET`, pinned). `web` has no route to the internet.
- **Client address.** Caddy trusts no client-sent `X-Forwarded-*` header, so rate limits see the address Caddy observed.
- **Logs.** Caddy's filter replaces token paths (`/accounts/password/reset/key/…`, and the invitation and email-change routes from M2.6) and every query string with `REDACTED` and drops `Referer` and `HX-Current-URL`. Gunicorn's access log is off; its error log goes through the same redaction as Django's.
- **Admin.** `ADMIN_URL_PATH` sets the admin prefix for both Caddy and Django; `ADMIN_ALLOWED_CIDRS` restricts it at Caddy (everyone else gets a 404). The admin also needs an MFA-verified session.
- **Database roles.** `owner` (migrations only), `runtime` (DML only, so it cannot alter or drop tables), `backup` (`pg_read_all_data`, read-only sessions). Passwords are generated by `init.sh` and refused if weak.
- **Mail.** Requests never talk to SMTP: they write `EmailOutbox` rows and the worker sends them. Sensitive bodies (reset links) are encrypted with `OUTBOX_ENCRYPTION_KEYS` and wiped once sent or after an hour. The SMTP password reaches the worker only.
- **Secrets.** The restic repository for offsite backups must be **append-only** from this host; `forget`/`prune` run only from the maintainer's machine with other credentials (plan §9).
- **Docker and ufw.** Published Docker ports bypass ufw. Only Caddy publishes anything; keep it that way.

## Tests

`tests/live` exercises a running stack (`LIVE_REQUIRED=1 uv run pytest -m live`, see `tests/live/conftest.py`). The `compose-smoke` workflow runs it on every pull request that touches the stack, and always on `main` and nightly.
