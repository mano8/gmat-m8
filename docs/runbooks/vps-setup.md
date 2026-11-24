# VPS setup

From an empty Debian 13 server to a stack that is ready for its first `./deploy.sh`. It is the work of step H8.6; the first release, v0.1.0, which opens the pilot (H8.7, [pilot.md](pilot.md)), comes after it. Allow half a day, most of it waiting for DNS, the SMTP account and the offsite storage.

The server runs **Docker rootless** ([ADR-0033](../adr/0033-rootless-docker-host.md)): the daemon is a service of the `deploy` account, there is no root daemon, and nobody is in the `docker` group. That group is root on the machine (Docker says so), so a host that has it makes the account that deploys, and anything that escapes a container, root. Step 4 sets this up; [../rootless-host.md](../rootless-host.md) has the design and the facts behind it.

Who does this: the maintainer, by hand. Agents never touch the server.

## What you need before you start

| Thing | Notes |
| --- | --- |
| A VPS | Debian 13 (cgroup v2 and `systemd`'s user services, which the rootless daemon needs, are its defaults), **x86-64**, **2 vCPU / 4 GB** with the swap of step 1, 40 GB of disk or more. **Not ARM:** the malware scanner's image is x86-64 only ([ADR-0015](../adr/0015-upload-scanning-and-decoder-isolation.md)). The stack idles at about 1.4 GB, 950 MB of it the scanner's signatures, and reaches about 3.2 GB with two images decoding; twelve sign-ins at the same instant take `web` to about 1.5 GB (Argon2 holds 100 MB per password checked, and its limit is 2 GB), so 4 GB works with little headroom and the swap of step 1 absorbs such a moment. 8 GB is the comfortable size if the price allows. |
| A domain | An `A` (and `AAAA`) record for the public name, for example `gmat.example.org`, pointing at the VPS. Caddy gets its certificate from Let's Encrypt on the first request, so the record must resolve **before** the first deploy. |
| An SMTP account | A relay that accepts submission on port 587 with STARTTLS and a certificate that verifies. `check --deploy` refuses a relay with credentials and no TLS (`gmat.E021`), so a deploy stops on that. The address you send from must be allowed by the relay. |
| Offsite storage | Two credential sets, see [../../docker_compose/offsite/README.md](../../docker_compose/offsite/README.md): **append-only** for the VPS, **delete-capable** for the maintainer's machine only. The storage lives on a host or provider that is not this VPS. |
| Access to the images | **No token: both image packages are public** ([ADR-0017](../adr/0017-signed-release-facts-and-public-image-packages.md)). GHCR creates them private at the first publish, so the release-day steps below make them public. The release manifests come from the public repository, so they need no token either. |
| An uptime monitor | Any external service that calls `https://<domain>/healthz/` every minute and tells you by a channel other than this server's email. |
| Offline storage | A password manager or encrypted drive that is not on the VPS, for the keys in step 7. |

## 1. Create the server and the `deploy` user

1. Create the VPS with your SSH public key. Note its IPv4 and IPv6 addresses.
2. Log in as root once and create the account that runs everything. Its uid **must be 1000**. The containers run as uid 1000, and the scripts, the stack's checks and the runbooks all assume one account for both kinds of daemon (a developer's or CI's root daemon needs it):
   - `flags/` belongs to `deploy`: Caddy and the worker only read it, and the scripts write it.
   - `backups/` belongs to the **containers'** uid 1000. On a root daemon that is `deploy` itself. On the rootless daemon of this runbook it is a shifted host uid (100999 with the usual range, below): the dumps are in a 0700 directory that `deploy` cannot read directly, and the scripts reach them through one checked helper (`backups_do` in `ops/common.sh`). Nobody gives them to `deploy` or loosens their mode, and you never enter the daemon's namespace by hand.

   ```bash
   adduser --uid 1000 --disabled-password --gecos "" deploy
   install -d -m 700 -o deploy -g deploy /home/deploy/.ssh
   cp ~/.ssh/authorized_keys /home/deploy/.ssh/authorized_keys
   chown deploy:deploy /home/deploy/.ssh/authorized_keys && chmod 600 /home/deploy/.ssh/authorized_keys
   id deploy        # uid=1000(deploy)
   ```

   If uid 1000 is taken (some images ship a default user), rename that user rather than choosing another number.

   The rootless daemon also needs **subordinate ids**: at least 65,536 uids and 65,536 gids for `deploy`. Debian normally allocates them when it creates the account; check, and add them if the lines are missing or shorter:

   ```bash
   grep '^deploy:' /etc/subuid /etc/subgid      # deploy:100000:65536 in both
   # only if missing:  usermod --add-subuids 100000-165535 --add-subgids 100000-165535 deploy
   ```

   They map the containers' ids onto the host: container uid 0 is `deploy`, and container uid N ≥ 1 is host uid 100000 + N − 1 (the containers' 1000 is 100999).
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

   **ufw protects the published ports here.** A rootless daemon does not write firewall rules: the port it publishes is a process on the host that listens like any other, so its traffic passes ufw's INPUT chain (a root daemon's rules go ahead of ufw's, and ufw would not see it). Allow only 22/tcp, 80/tcp, 443/tcp and 443/udp, as above; nothing else may be reachable. The stack still publishes nothing but Caddy's two ports (never add a `ports:` entry for another service, never publish the database), and a port is published once (`0.0.0.0:443`), not once per host address, so who reaches it is ufw's decision alone.
3. Optional: `apt-get install -y fail2ban` for the SSH log-in.

## 3. Updates, time and logs

```bash
apt-get install -y unattended-upgrades chrony logrotate
dpkg-reconfigure -plow unattended-upgrades      # yes
systemctl enable --now chrony
timedatectl                                     # System clock synchronized: yes
```

- **Time matters.** Grants are valid between two timestamps, tokens expire, and offsite snapshots carry the VPS's clock; `scripts/offsite_prune.py` refuses a snapshot dated in the future.
- **Reboots.** `unattended-upgrades` installs security updates but does not reboot unless you set `Unattended-Upgrade::Automatic-Reboot`. Leave it off and reboot by hand, in a maintenance window, when `/var/run/reboot-required` exists. The containers use `restart: unless-stopped` and the rootless daemon starts at boot (linger, §4.1), so the stack comes back by itself (the gates are files, so a reboot during a deploy comes back **gated**). After the first reboot, log in over SSH and check that `docker compose ps` shows the stack up: nobody started the daemon, so this is what linger is for.
- **Logs.** The compose file rotates every container's log at 10 MB × 3 files. Nothing else to configure for the stack; `journald` and the system logs are handled by Debian's defaults.

## 4. Install rootless Docker Engine, `cosign` and the other tools

### 4.1 Docker, rootless

The daemon runs as `deploy`, as one of its `systemd` user services. Docker's own page for it is <https://docs.docker.com/engine/security/rootless/>: read it once, the commands below follow it. Points 1 to 4 are the root's (or `sudo`'s); points 5 to 7 are `deploy`'s. **For `deploy`'s, log in through SSH** (not `su` or `sudo -u`): the daemon needs the login session's user `systemd` and its `XDG_RUNTIME_DIR` (`/run/user/1000`), and `echo "$XDG_RUNTIME_DIR"` must print that path.

1. **Docker's packages.** Docker Engine and the Compose plugin come from **Docker's own repository**, not Debian's. Follow the current instructions at <https://docs.docker.com/engine/install/debian/> (the apt repository method) and install `docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin`, plus the rootless pieces:

   ```bash
   apt-get install -y docker-ce-rootless-extras uidmap dbus-user-session slirp4netns
   ```

   `docker-ce-rootless-extras` has the setup tool and `dockerd-rootless.sh`, `uidmap` the `newuidmap` and `newgidmap` that apply the subordinate ids of §1, `dbus-user-session` the user's `systemd`, `slirp4netns` the network driver of point 5.
2. **No root daemon, no `docker` group member.** Docker's package starts a root daemon. Turn it off and remove its socket, or the setup tool needs `--force` and two daemons run:

   ```bash
   systemctl disable --now docker.service docker.socket
   rm -f /var/run/docker.sock
   ```

   The package creates the group `docker`. It stays, **empty**: do not `usermod -aG docker deploy`, now or ever. Whoever is in it is root on this machine.
3. **Ports 80 and 443 (Q74).** A rootless daemon may not listen below port 1024 by default. Let any account listen from 80 up:

   ```bash
   echo 'net.ipv4.ip_unprivileged_port_start=80' > /etc/sysctl.d/91-gmat-ports.conf
   sysctl --system | grep ip_unprivileged_port_start       # = 80
   ```

   Ports below 80 (SSH on 22, SMTP, DNS) stay privileged, and on this server the only other processes are system daemons that cannot read Caddy's key. A `setcap` on `rootlesskit` would be narrower, but a package upgrade replaces the binary and drops it, and 443 would stop answering at the next restart of the daemon: that is why it is not used here ([ADR-0033](../adr/0033-rootless-docker-host.md), point 3).
4. **Start at boot.** The daemon is a user service, so without linger it stops at logout and does not start after a reboot, and the stack with it:

   ```bash
   loginctl enable-linger deploy
   ```

5. **Install the daemon, as `deploy`**, in a fresh SSH login:

   ```bash
   dockerd-rootless-setuptool.sh install      # starts docker.service (user), creates and selects the CLI context "rootless"
   ```

   Then choose the **network and port drivers**. The default port driver (`builtin`) shows every visitor to Caddy as one internal address (or, from RootlessKit 3.0, keeps the address for TCP only and never for UDP). The application limits sign-in attempts **per address** and writes the address in its audit log and its session list, so with one address for everybody a single attacker's failed sign-ins would lock all the users out, and the log would mean nothing. `slirp4netns` keeps the visitor's address (and carries UDP 443, HTTP/3). Create `~/.config/systemd/user/docker.service.d/override.conf`:

   ```bash
   mkdir -p ~/.config/systemd/user/docker.service.d
   cat > ~/.config/systemd/user/docker.service.d/override.conf <<'CONF'
   [Service]
   Environment="DOCKERD_ROOTLESS_ROOTLESSKIT_NET=slirp4netns"
   Environment="DOCKERD_ROOTLESS_ROOTLESSKIT_PORT_DRIVER=slirp4netns"
   CONF
   systemctl --user daemon-reload && systemctl --user restart docker
   ```

   `deploy.sh` reads the driver of the running daemon before it fetches a release and **refuses** `builtin`, any driver that does not carry UDP, and any it has not been told about ([../rootless-host.md](../rootless-host.md)). The other driver it accepts, `implicit` with the `pasta` network driver, is experimental in Docker and not part of this runbook.
6. **Reach the daemon from every session.** The setup tool makes `rootless` the CLI's current context, which is stored in `~/.docker/config.json` and read by every session of `deploy`, interactive or not. Check both ways, from your own machine, not only from a shell you are already in:

   ```bash
   ssh deploy@<server> 'docker context show'                                     # rootless
   ssh deploy@<server> 'docker info --format "{{.SecurityOptions}}"'             # ... name=rootless ...
   ```

   If the second is not `rootless` (a `DOCKER_HOST` pointing elsewhere, or no context), export it where a non-interactive SSH command reads it. Debian's `~/.bashrc` stops early for a non-interactive shell, so put the line **above** that guard (bash started by `sshd` reads this file):

   ```bash
   export DOCKER_HOST="unix://$XDG_RUNTIME_DIR/docker.sock"
   ```

   The scripts refuse a CLI that talks to anything but this socket (`tcp://`, `ssh://`, another account's), so a wrong value stops them instead of misleading them.
7. **Check the host**, as `deploy`. Every line must give the answer in its comment:

   ```bash
   docker info --format '{{.SecurityOptions}}'        # contains name=rootless
   docker run --rm hello-world
   docker compose version
   ls /var/run/docker.sock                            # No such file or directory
   systemctl is-enabled docker.service docker.socket  # disabled, disabled  (the root daemon's units)
   getent group docker                                # docker:x:<gid>:       nobody after the last colon
   id -Gn | tr ' ' '\n' | grep -cx docker             # 0
   loginctl show-user deploy -p Linger                # Linger=yes
   sysctl net.ipv4.ip_unprivileged_port_start         # = 80
   ```

   The client's address is the last thing to check, and it needs the application: it is in §8, after the first deploy. `./recover.sh status` already shows the port driver once the checkout of step 5 exists.

### 4.2 The tools of the scripts

The deploy scripts need `docker`, `curl`, `flock`, `timeout`, `cosign`, `python3`, `nsenter` and `sha256sum` (`deploy.sh` stops with "X is not installed" otherwise). On Debian 13:

```bash
apt-get install -y curl git python3 util-linux coreutils
```

`flock`, `timeout` and `nsenter` come from `util-linux` and `coreutils`; Python 3 is only used for small JSON checks (`ops/checks.py`), no packages needed. `nsenter` is what the scripts' helper uses to read the dumps under `backups/`.

### 4.3 `cosign`

Every release is verified with it, so install it from the project's release page and check the download against the published checksums. It must be **v3.0.6 or newer**: the release workflow signs with cosign 3 (`sigstore/cosign-installer` v4), whose signatures cosign 2 does not find ("no signatures found"). `deploy.sh` reads `cosign version` before it fetches a release and stops on anything older than 3 ("cosign 3 or newer is required"), before changing anything.

```bash
VER=v3.0.6
cd "$(mktemp -d)"
curl -fsSLO "https://github.com/sigstore/cosign/releases/download/$VER/cosign-linux-amd64"
curl -fsSLO "https://github.com/sigstore/cosign/releases/download/$VER/cosign_checksums.txt"
sha256sum --ignore-missing -c cosign_checksums.txt     # must say: cosign-linux-amd64: OK
sudo install -m 0755 cosign-linux-amd64 /usr/local/bin/cosign
cosign version                                       # GitVersion: v3.0.6 or newer
```

### 4.4 A host that already runs the root daemon

Volumes do not move from one daemon to the other, so the data travels through a backup: **dump, set up rootless, restore.** It is [the loss of a VPS](backup-restore.md#the-vps-is-gone) on the same machine, with the same price: up to 6 hours of data are gone unless you stopped the writes first (below), every privileged account comes up suspended until you confirm it, and everybody signs in again. Do it in a maintenance window, and expect some hours.

1. Stop the writes and take the last dump: `touch flags/maintenance.on flags/worker.paused`, then `dc exec backup backup-now --label before-rootless`. Confirm from your machine that the snapshot is in the offsite repository and note its id ([backup-restore.md](backup-restore.md)). Have `.env`, `app.env` and the restic password in the offline storage.
2. `dc down`, **without** `--volumes`: the old volumes are the way back until the end. Remove `deploy` from the `docker` group (`gpasswd -d deploy docker`), then do §4.1's points 1 to 7, including turning the root daemon off.
3. `bash init.sh`. It keeps the env files, gives `backups/` and what is in it (the dumps that `deploy` owned under the root daemon) to the containers' shifted uid, so the `backup` container can read them, and checks the daemon. `./recover.sh status` must now say the daemon is rootless and name its port driver.
4. Restore the snapshot of step 1 onto the new, empty daemon, as for a lost VPS. No `deploy.sh` first: `restore-offsite` starts the stack itself, behind both gates, from the signed release you name (the version that ran, the last line of `deployed.log`):

   ```bash
   ./recover.sh restore-offsite --snapshot <id> --version X.Y.Z --operator you@example.org \
     --incident "moved to rootless Docker"
   ```

   Then follow [After any restore](backup-restore.md#after-any-restore): confirm each suspended account, release or discard the held email, and tell the people.
5. Once the site is checked and a drill has passed, remove the old daemon's data, as root: `rm -rf /var/lib/docker`. Until then it is the way back.

## 5. Get the code

The compose files, scripts and Caddyfile come from the repository, at the **release's own commit**. `deploy.sh` refuses a checkout that is not that commit.

```bash
git clone https://github.com/<owner>/gmat-m8.git ~/gmat-m8        # a public repository: no token needed
cd ~/gmat-m8/docker_compose/gmat_m8
```

Keep the `deploy` user's default umask (022): the database and the scanner read their files from this checkout under their own uids (shifted ones, under the rootless daemon). `init.sh` opens them up once, but a later `git switch` writes changed files with whatever umask is set.

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
| `DJANGO_ADMIN_ENABLED` | leave `false`. Everything is administered in the app; the Django admin is an optional, read-only view of the records (ADR-0030). Off, no admin URL exists and the two keys below do not matter. Turn it on (`true`) only to inspect something, together with `ADMIN_ALLOWED_CIDRS`, then run `./deploy.sh` with the running version again; turn it off the same way |
| `ADMIN_URL_PATH` | something not guessable, no slashes. Not secret, but it keeps scanners away. Used only while the admin is on |
| `ADMIN_ALLOWED_CIDRS` | needed **only while the admin is on**, and then there is no way around it. The addresses that may reach the admin, for example your office's fixed address: `203.0.113.7/32`. While the admin is on, `deploy.sh` refuses an empty list, `0.0.0.0/0`, and also any range that overlaps Docker's bridges (`172.16.0.0/12`, `192.168.0.0/16`), because behind Docker's proxy a visitor can look like one of them; Caddy lets nobody in with an empty list. Exception X10. Leave it empty while the admin is off |
| `GITHUB_READ_TOKEN` | leave empty. Only a fork with private packages sets it, to a **classic** token with only `read:packages`: GHCR refuses a fine-grained one after a successful login |
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
| `PO_DISPATCH` | `manual` (the template): buyers send an issued order to its supplier from the order's page; `off` prints only; `on` also emails at each issue and amendment (ADR-0010 Q7). An upgrade keeps the value you have: set `manual` by hand to offer the button, and send one order to your own address first ([email.md](email.md#supplier-dispatches)) |
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
| `SMTP_PASSWORD` | `.env` | Issue a new one. |

A full copy of `.env` and `app.env` in the offline storage is the simplest way to cover all rows. The offsite restic repository holds the data but **not** these files, by design.

## 8. First start and first checks

The first deploy is part of H8.7, after the first release (v0.1.0) exists. Until then you can check everything that does not need images:

```bash
cd ~/gmat-m8/docker_compose/gmat_m8
stat -c '%a %n' .env app.env                       # 600 for both
ls -ld flags                                       # owned by deploy, mode 0755 (the worker only reads it, as a shifted uid)
ls -ld backups                                     # rootless: owned by 100999 (the containers' uid 1000), drwx------; deploy cannot list it, and must not
./recover.sh status                                # "daemon: rootless" and the port driver (slirp4netns)
getent group docker                                # still nobody in it
python3 ops/checks.py admin-cidrs "$(sed -n 's/^ADMIN_ALLOWED_CIDRS=//p' .env)" \
    --enabled "$(sed -n 's/^DJANGO_ADMIN_ENABLED=//p' .env)" && echo ok   # "the admin is off" while it is
dig +short gmat.example.org                        # the VPS's address
cosign version && docker compose version && flock --version | head -1
```

`ls -ld backups` shows the owner as a number, because `deploy` has no name for a shifted uid. Do not `chown` or `chmod` it to make it readable: `deploy.sh` refuses a `backups/` that is not the containers' uid 1000 and mode 0700, and `bash init.sh` is the one thing that sets it right.

**Before the first deploy, make the images public.** The first publish creates both packages private. On GitHub, open each of `gmat-m8` and `gmat-m8-backup` under the owner's packages → **Package settings** → **Change visibility** → **Public**. It cannot be undone. Then check from the VPS, without logging in:

```bash
docker logout ghcr.io
docker manifest inspect ghcr.io/<owner>/gmat-m8:X.Y.Z >/dev/null && \
docker manifest inspect ghcr.io/<owner>/gmat-m8-backup:X.Y.Z >/dev/null && echo "both public"
```

On the release day follow [deploy-rollback.md](deploy-rollback.md). The very first deploy has no previous version: `deploy.sh` skips the checks that need one, takes the pre-deploy backup (which also creates the offsite repository), and records the first line in `deployed.log`. Then:

1. Create the first administrator, a **platform operator**, so the recovery tooling has someone to run as ([user-admin.md](user-admin.md#the-first-administrator)).
2. **Check the visitor's address**, now that Caddy answers. From **another machine** (your laptop on another network, a phone on mobile data), open `https://<domain>/` and sign in, then open the session list at `/account/sessions/`. The address shown for the session must be that machine's own public address. An address of `10.0.2.x`, `10.x` or `172.x` means the driver hides it: the sign-in limits then count everybody as one, so enough failed attempts from one person lock all the others out. Stop, go back to §4.1, point 5, and check `./recover.sh status`.
3. Run the first offsite drill ([backup-restore.md](backup-restore.md#quarterly-offsite-drill)) before real data goes in.
4. Set up monitoring (next section).

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
| The daemon | `systemctl --user is-active docker`, as `deploy` (the external uptime check notices first: everything stops with it) | not `active`. `journalctl --user -u docker --since "1 hour ago"` says why; `systemctl --user restart docker` brings it back, and the containers with it |
| Disk | a cron job, or the provider's alert | above 80 % (`df -h /`, `docker system df`). Docker's data root is `~deploy/.local/share/docker`, on the filesystem of `/home`: watch that one, and note that `du` cannot read into it as `deploy` (subordinate uids), so `docker system df` is the measure |
| Uploads | `dc ps`: `clamd` and `decoder` healthy. `dc exec clamd clamdscan --version` shows the signatures' date | unhealthy: every upload is refused until they are back, everything else works. Signatures older than 3 days: read `dc logs freshclam` (the mirror answers from outside; ufw allows outgoing) |
| The images | the nightly `image-scan` workflow opens an issue | an open issue: see [vulnerability-exceptions.md](vulnerability-exceptions.md) |
| Backup verification | weekly, automatic (`restore --verify`, Sunday 03:30 UTC) | `docker compose logs backup` shows a failure; `/backups/.last-verify-ok` is older than 8 days (`dc exec backup ls -l /backups/.last-verify-ok`: `deploy` cannot list `backups/` itself) |

Reviews to put in the calendar: the **quarterly** drill; the exception register and `.trivyignore.yaml` (**every 90 days at most**); the `sudo` right of `deploy` (remove it once setup is done: `deluser deploy sudo`, using root's console when you need it again).

## Done when

- You can log in as `deploy` with the key only; root and passwords are refused.
- `ufw status` shows 22/tcp, 80/tcp, 443/tcp and 443/udp only; nothing else listens (`ss -tlnp`, `ss -ulnp`).
- Docker is rootless and nobody is in the `docker` group: `docker info` names `rootless` over a non-interactive SSH command too, `/var/run/docker.sock` does not exist, `getent group docker` lists nobody, and `loginctl show-user deploy -p Linger` says `yes`.
- A session from another machine shows that machine's own address in `/account/sessions/` (§8).
- The server is x86-64 and has swap (`uname -m`, `swapon --show`).
- `.env` and `app.env` are mode 0600, filled in, and copied to the offline storage together with the restic password.
- The checks of step 8 pass: DNS resolves, and the admin is off (or, if you turned it on, its allowlist is accepted).
- The uptime monitor exists and you know what each alert means.
