# VPS setup

From an empty Debian 13 server to a stack that is ready for its first `./deploy.sh`. It is the work of step H8.6; the first release (H8.7) comes after it. Allow half a day, most of it waiting for DNS, the SMTP account and the offsite storage.

Who does this: the maintainer, by hand. Agents never touch the server.

## What you need before you start

| Thing | Notes |
| --- | --- |
| A VPS | Debian 13, **x86-64**, **2 vCPU / 4 GB** with the swap of step 1, 40 GB of disk or more. **Not ARM:** the malware scanner's image is x86-64 only ([ADR-0015](../adr/0015-upload-scanning-and-decoder-isolation.md)). The stack idles at about 1.4 GB, 950 MB of it the scanner's signatures, and reaches about 3.2 GB with two images decoding, so 4 GB works with little headroom. 8 GB is the comfortable size if the price allows. |
| A domain | An `A` (and `AAAA`) record for the public name, for example `gmat.example.org`, pointing at the VPS. Caddy gets its certificate from Let's Encrypt on the first request, so the record must resolve **before** the first deploy. |
| An SMTP account | A relay that accepts submission on port 587 with STARTTLS and a certificate that verifies. `check --deploy` refuses a relay with credentials and no TLS (`gmat.E021`), so a deploy stops on that. The address you send from must be allowed by the relay. |
| Offsite storage | Two credential sets, see [../../docker_compose/offsite/README.md](../../docker_compose/offsite/README.md): **append-only** for the VPS, **delete-capable** for the maintainer's machine only. The storage lives on a host or provider that is not this VPS. |
| Access to the images | GHCR creates the two packages **private** at the first publish, and takes only a **classic** personal access token: a fine-grained one logs in, then is denied on the pull. Choose one (H8.6). **Public packages:** change the visibility of both after the first publish (package settings; it cannot be undone) and leave `GITHUB_READ_TOKEN` empty, which keeps a credential off the server. The source is public already. **Or a classic token with only `read:packages`:** GitHub cannot limit it to this repository, so it reads every private package the account can. It expires, and an expired one stops a deploy or a recovery at the pull, so put the renewal date in your calendar. The release manifests are read from the public repository and need no token. |
| An uptime monitor | Any external service that calls `https://<domain>/healthz/` every minute and tells you by a channel other than this server's email. |
| Offline storage | A password manager or encrypted drive that is not on the VPS, for the keys in step 7. |

## 1. Create the server and the `deploy` user

1. Create the VPS with your SSH public key. Note its IPv4 and IPv6 addresses.
2. Log in as root once and create the account that runs everything. Its uid **must be 1000**: the containers run as uid 1000, and the bind-mounted `backups/` and `flags/` directories belong to that uid.

   ```bash
   adduser --uid 1000 --disabled-password --gecos "" deploy
   install -d -m 700 -o deploy -g deploy /home/deploy/.ssh
   cp ~/.ssh/authorized_keys /home/deploy/.ssh/authorized_keys
   chown deploy:deploy /home/deploy/.ssh/authorized_keys && chmod 600 /home/deploy/.ssh/authorized_keys
   id deploy        # uid=1000(deploy)
   ```

   If uid 1000 is taken (some images ship a default user), rename that user rather than choosing another number.
3. Give `deploy` administrative rights through `sudo` only for the setup; step 9 reviews this.

   ```bash
   apt-get update && apt-get install -y sudo
   usermod -aG sudo deploy
   ```

4. Add swap, so a burst of memory slows the server instead of killing a container:

   ```bash
   fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
   echo '/swapfile none swap sw 0 0' >> /etc/fstab
   sysctl -w vm.swappiness=10 && echo 'vm.swappiness=10' > /etc/sysctl.d/90-gmat.conf
   ```

## 2. Harden SSH and the firewall

1. Keys only, no root login. Create `/etc/ssh/sshd_config.d/10-gmat.conf`:

   ```text
   PermitRootLogin no
   PasswordAuthentication no
   KbdInteractiveAuthentication no
   AllowUsers deploy
   ```

   Open a **second** terminal and log in as `deploy` with the key before you reload, so a mistake does not lock you out:

   ```bash
   sshd -t && systemctl reload ssh
   ```

2. Firewall: 22, 80 and 443 only (443 for TCP and UDP: HTTP/3).

   ```bash
   apt-get install -y ufw
   ufw default deny incoming && ufw default allow outgoing
   ufw allow 22/tcp && ufw allow 80/tcp && ufw allow 443/tcp && ufw allow 443/udp
   ufw enable && ufw status verbose
   ```

   **ufw does not protect published Docker ports**: Docker writes its own rules ahead of ufw's. This is why only Caddy publishes anything in this stack. Never add a `ports:` entry for another service, and never publish the database.
3. Optional: `apt-get install -y fail2ban` for the SSH log-in.

## 3. Updates, time and logs

```bash
apt-get install -y unattended-upgrades chrony logrotate
dpkg-reconfigure -plow unattended-upgrades      # yes
systemctl enable --now chrony
timedatectl                                     # System clock synchronized: yes
```

- **Time matters.** Grants are valid between two timestamps, tokens expire, and offsite snapshots carry the VPS's clock; `scripts/offsite_prune.py` refuses a snapshot dated in the future.
- **Reboots.** `unattended-upgrades` installs security updates but does not reboot unless you set `Unattended-Upgrade::Automatic-Reboot`. Leave it off and reboot by hand, in a maintenance window, when `/var/run/reboot-required` exists. The containers use `restart: unless-stopped`, so the stack comes back by itself (the gates are files, so a reboot during a deploy comes back **gated**).
- **Logs.** The compose file rotates every container's log at 10 MB × 3 files. Nothing else to configure for the stack; `journald` and the system logs are handled by Debian's defaults.

## 4. Install Docker Engine, `cosign` and the other tools

1. Docker Engine and the Compose plugin come from **Docker's own repository**, not Debian's. Follow the current instructions at <https://docs.docker.com/engine/install/debian/> (the apt repository method), then:

   ```bash
   usermod -aG docker deploy        # log out and in again
   docker run --rm hello-world
   docker compose version
   ```

   Adding `deploy` to the `docker` group makes it root-equivalent on this machine. That is accepted: `deploy` is the only login, and it is key-only.
2. The deploy scripts need `docker`, `curl`, `flock`, `timeout`, `cosign`, `python3` and `sha256sum` (`deploy.sh` stops with "X is not installed" otherwise). On Debian 13:

   ```bash
   apt-get install -y curl git python3 util-linux coreutils
   ```

   `flock` and `timeout` come from `util-linux` and `coreutils`; Python 3 is only used for small JSON checks (`ops/checks.py`), no packages needed.
3. **`cosign`.** Every release is verified with it, so install it from the project's release page and check the download against the published checksums. The deploy was rehearsed with v2.5.0 or newer.

   ```bash
   VER=v2.5.0
   cd "$(mktemp -d)"
   curl -fsSLO "https://github.com/sigstore/cosign/releases/download/$VER/cosign-linux-amd64"
   curl -fsSLO "https://github.com/sigstore/cosign/releases/download/$VER/cosign_checksums.txt"
   sha256sum --ignore-missing -c cosign_checksums.txt     # must say: cosign-linux-amd64: OK
   sudo install -m 0755 cosign-linux-amd64 /usr/local/bin/cosign
   cosign version
   ```

## 5. Get the code

The compose files, scripts and Caddyfile come from the repository, at the **release's own commit**. `deploy.sh` refuses a checkout that is not that commit.

```bash
git clone https://github.com/<owner>/gmat-m8.git ~/gmat-m8        # a public repository: no token needed
cd ~/gmat-m8/docker_compose/gmat_m8
```

The tooling assumes the owner `mano8` and images under `ghcr.io/mano8` (`ops/common.sh`, `docker-compose.production.yml`). A fork must change both.

## 6. Create the env files

```bash
bash init.sh        # creates .env and app.env with generated secrets, mode 0600; prints no secret
```

`init.sh` writes development values for the public address, mail and ports. Edit both files for production. Keep them at mode 0600 (`deploy.sh` refuses anything else), and never commit them.

### `.env` (compose-level)

| Key | Set to |
| --- | --- |
| `APP_DOMAIN` | the public name, for example `gmat.example.org` |
| `ACME_EMAIL` | an address that gets Let's Encrypt's expiry notices |
| `ADMIN_URL_PATH` | something not guessable, no slashes. Not secret, but it keeps scanners away |
| `ADMIN_ALLOWED_CIDRS` | **required.** The addresses that may reach the admin, for example your office's fixed address: `203.0.113.7/32`. There is no way around this one: the production overlay needs the value, and `deploy.sh` refuses `0.0.0.0/0`, and also any range that overlaps Docker's bridges (`172.16.0.0/12`, `192.168.0.0/16`), because behind Docker's proxy a visitor can look like one of them. Exception X10 |
| `GITHUB_READ_TOKEN` | empty with public packages, otherwise the classic token from "What you need". Read only by `deploy.sh` and `recover.sh` |
| `SMTP_PASSWORD` | the SMTP account's password. Only the worker receives it |
| `RESTIC_REPOSITORY`, `RESTIC_PASSWORD`, `RESTIC_REST_USERNAME`, `RESTIC_REST_PASSWORD` (or `AWS_*` for S3) | the **append-only** credentials. `RESTIC_REPOSITORY` must not be empty: `deploy.sh` refuses |
| `BACKUP_HEARTBEAT_URL` | optional: a dead-man's-switch URL (healthchecks-style) pinged after every successful dump |
| `COMPOSE_PATH_SEPARATOR`, `COMPOSE_FILE` | **delete both lines**: they add Mailpit, the local mail sink |
| `BIND_IP`, `HTTP_PORT`, `HTTPS_PORT` | leave: the production overlay forces 80 and 443 |

The database passwords, `POSTGRES_PASSWORD` and the three role passwords, were generated by `init.sh`. Keep them; they are also in the offline copy (step 7).

### `app.env` (Django)

Start from the production template, which has no usable defaults:

```bash
cp app.env app.env.dev-values && cp app.env.production.example app.env && chmod 600 app.env
```

Then fill it in (the application refuses to start on a missing, weak or placeholder value):

| Key | Set to |
| --- | --- |
| `DJANGO_SECRET_KEY` | `openssl rand -base64 60 \| tr -d '\n=+/'` |
| `MFA_ENCRYPTION_KEYS` | `openssl rand -base64 32 \| tr '+/' '-_'` (one key, newest first; more later when you rotate) |
| `OUTBOX_ENCRYPTION_KEYS` | generated the same way, and **not equal** to the MFA key (the app checks) |
| `DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS` | `gmat.example.org` and `https://gmat.example.org`, exactly as users type them |
| `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `DJANGO_DEFAULT_FROM_EMAIL` | the relay's host, `587`, its user, and the From address |
| `OUTBOUND_EMAIL` | `on` (the template default). Set `off` to keep a stack silent while you test |
| `PO_DISPATCH` | `off` at first: purchase orders are printed, not emailed to suppliers (ADR-0010 Q7). Turn on only when suppliers have agreed to receive them |
| `DJANGO_HSTS_SECONDS`, `DJANGO_HSTS_INCLUDE_SUBDOMAINS`, `DJANGO_HSTS_PRELOAD` | leave `0`, `false`, `false` for the first deploy; [turn them on](#turning-on-hsts) once the domain is proven. `web` logs a warning at every start until then |
| `DJANGO_SESSION_IDLE_SECONDS`, `DJANGO_PRIVILEGED_SESSION_IDLE_SECONDS` | how long a session may stay idle: the template's 2 hours and 30 minutes, unless you have a reason |

Delete `app.env.dev-values` afterwards: it holds the development secrets.

## 7. Keep the keys somewhere else

The following exist only on the VPS right now. Copy each to the offline storage **before the first deploy**, and say in your records who holds the copy.

| Secret | Where it lives | If you lose it |
| --- | --- | --- |
| `RESTIC_PASSWORD` | `.env` | **Every offsite backup is unreadable.** There is no recovery. |
| `MFA_ENCRYPTION_KEYS` | `app.env` | Every stored authenticator becomes unreadable: all users re-enroll their second factor ([user-admin.md](user-admin.md)). |
| `OUTBOX_ENCRYPTION_KEYS` | `app.env` | Messages sealed at that moment fail and are re-sent by hand; nothing else is lost ([user-admin.md](user-admin.md)). |
| `DJANGO_SECRET_KEY` | `app.env` | Sessions and signed links end. Replaceable at will; a restore replaces it anyway. |
| `POSTGRES_PASSWORD` and the role passwords | `.env` | Reset by recreating the roles; keep them for a restore onto the same compose project. |
| The append-only and the delete-capable offsite credentials | `.env` / maintainer's machine | Issue new ones at the storage provider. |
| `GITHUB_READ_TOKEN`, `SMTP_PASSWORD` | `.env` | Issue new ones. |

A full copy of `.env` and `app.env` in the offline storage is the simplest way to cover all rows. The offsite restic repository holds the data but **not** these files, by design.

## 8. First start and first checks

The first deploy is part of H8.7, after the first release exists. Until then you can check everything that does not need images:

```bash
cd ~/gmat-m8/docker_compose/gmat_m8
stat -c '%a %n' .env app.env                       # 600 for both
ls -ld backups flags                               # owned by deploy (uid 1000); backups is 0700
python3 ops/checks.py admin-cidrs "$(sed -n 's/^ADMIN_ALLOWED_CIDRS=//p' .env)" && echo ok
dig +short gmat.example.org                        # the VPS's address
cosign version && docker compose version && flock --version | head -1
# Only with a token. A fine-grained one prints nothing here: GHCR would refuse it.
curl -fsSI -H "Authorization: Bearer $(sed -n 's/^GITHUB_READ_TOKEN=//p' .env)" \
  https://api.github.com/user | grep -i '^x-oauth-scopes: read:packages'
```

On the release day follow [deploy-rollback.md](deploy-rollback.md). The very first deploy has no previous version: `deploy.sh` skips the checks that need one, takes the pre-deploy backup (which also creates the offsite repository), and records the first line in `deployed.log`. Then:

1. Create the first administrator, a **platform operator**, so the recovery tooling has someone to run as ([user-admin.md](user-admin.md#the-first-administrator)).
2. Run the first offsite drill ([backup-restore.md](backup-restore.md#quarterly-offsite-drill)) before real data goes in.
3. Set up monitoring (next section).

### Turning on HSTS

HSTS is yours to turn on, by hand: a browser that has seen the header refuses plain HTTP for as long as the header said, **and nothing on the server can take that back**. Do it after the first deploy, once HTTPS has worked on the real domain for some days and the certificate has renewed at least once if you can wait that long. One switch at a time, in `app.env`, then `dc up -d --no-deps --force-recreate web` behind nothing (about 15 seconds):

1. `DJANGO_HSTS_SECONDS=300`. Check the header with `curl -sI https://<domain>/accounts/login/ | grep -i strict`, and that the site works.
2. Raise it, a step per week if you like: `86400`, then `31536000` (one year).
3. `DJANGO_HSTS_INCLUDE_SUBDOMAINS=true`, **only if every subdomain of the name serves HTTPS** (a forgotten `intranet.` stops working for whoever has seen the header).
4. `DJANGO_HSTS_PRELOAD=true`, and only then submit the domain to [the preload list](https://hstspreload.org/). Leaving the list takes months; skip this unless you mean it.

`web`'s log says which switches are still off at each start (`hsts_incomplete`); no deploy fails on it. When all that you intend are on, update exception X3 in `docs/security.md`.

## 9. Monitoring and reviews

| What | How | Alert when |
| --- | --- | --- |
| The site | external uptime check on `https://<domain>/healthz/` | not 200 for 2–3 minutes. During a deploy it answers the maintenance page (503): expect that |
| Backups | `BACKUP_HEARTBEAT_URL` | no ping for 7 hours (the dump runs every 6) |
| The worker | `docker compose exec worker /app/docker/app/entrypoint.sh worker-healthcheck` or `docker compose ps` (health) | unhealthy for 5 minutes. A paused worker (`flags/worker.paused`) still reports ready |
| The outbox | see [email.md](email.md#watching-the-outbox) | `QUEUED` older than 10 minutes, any `AMBIGUOUS` or `QUARANTINED` |
| Disk | a cron job, or the provider's alert | above 80 % (`df -h /`, `docker system df`) |
| Uploads | `dc ps`: `clamd` and `decoder` healthy. `dc exec clamd clamdscan --version` shows the signatures' date | unhealthy: every upload is refused until they are back, everything else works. Signatures older than 3 days: read `dc logs freshclam` (the mirror answers from outside; ufw allows outgoing) |
| The images | the nightly `image-scan` workflow opens an issue | an open issue: see [vulnerability-exceptions.md](vulnerability-exceptions.md) |
| Backup verification | weekly, automatic (`restore --verify`, Sunday 03:30 UTC) | `docker compose logs backup` shows a failure; `backups/.last-verify-ok` is older than 8 days |

Reviews to put in the calendar: the GitHub token's expiry; the **quarterly** drill; the exception register and `.trivyignore.yaml` (**every 90 days at most**); the `sudo` right of `deploy` (remove it once setup is done: `deluser deploy sudo`, using root's console when you need it again).

## Done when

- You can log in as `deploy` with the key only; root and passwords are refused.
- `ufw status` shows 22, 80, 443 only; nothing else listens (`ss -tlnp`).
- The server is x86-64 and has swap (`uname -m`, `swapon --show`).
- `.env` and `app.env` are mode 0600, filled in, and copied to the offline storage together with the restic password.
- The checks of step 8 pass: DNS resolves, the admin allowlist is accepted and the token can read the repository.
- The uptime monitor exists and you know what each alert means.
