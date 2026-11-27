# ADR-0033: The host runs Docker rootless; no `docker` group

- **Status:** Accepted on 2026-11-27 by the maintainer, at the review of the M12 pull request, with Q74 (point 3) and Q75 (point 4) answered as below, exception X15 accepted and the worker's fail-closed pause flag (point 7) kept. Proposed in M12.1 from the maintainer's requirement of 2026-11-15
- **Date:** 2026-11-24
- **Governs:** `docs/runbooks/vps-setup.md`, `docs/rootless-host.md`, `docker_compose/gmat_m8/init.sh`, `docker_compose/gmat_m8/ops/common.sh`, `apps/notifications/worker.py`, `docker_compose/gmat_m8/docker-compose.production.yml`, `docker_compose/gmat_m8/.env.example`

## Context

The VPS runbook installed Docker's root daemon and added `deploy` to the `docker` group. Docker's own documentation says that group grants root: whoever can reach the socket can mount `/` into a container. A container escape under a root daemon is root on the host, too. The maintainer requires that the account that deploys is never root-equivalent, and that no container process is root on the host.

The pilot host (Debian 13, arm64) has run v0.1.0 on rootless Docker since 2026-11-15, with only a local overlay. `deploy.sh` and `recover.sh status` work there unchanged. Nothing that reads `backups/` from the host works: the dumps belong to a shifted uid in a 0700 directory, so `recover.sh` and `restore-drill.sh` cannot restore there. Someone on the host has to enter the daemon's user namespace by hand.

The facts this record relies on were checked against Docker's current rootless documentation and RootlessKit's own documentation on 2026-11-24. They are listed with their sources in [`docs/rootless-host.md`](../rootless-host.md); M12.5 measures the rest on an amd64 machine.

## Decision

1. **The production host runs Docker rootless, as `deploy`.** The daemon is `deploy`'s systemd user service (`dockerd-rootless-setuptool.sh install`, from Docker's `docker-ce-rootless-extras` package), started at boot through `loginctl enable-linger deploy`. Container uid 0 is `deploy`. Container uid N ≥ 1 is host uid `subuid_start + N − 1`: the containers' uid 1000 is 100999 with the usual range `100000:65536`. `deploy` has at least 65,536 subordinate uids and gids.
2. **No root daemon and no `docker` group member.** `docker.service` and `docker.socket` are disabled and `/var/run/docker.sock` is gone. The group Docker's package creates stays, empty: `getent group docker` lists nobody. Root on the host still needs `sudo` and its password.
3. **Ports 80 and 443 (Q74): the operator chooses one of two ways** (vps-setup.md §4.1, point 3). The maintainer asked for the choice on 2026-11-27, in place of the sysctl alone.
   - **A, the default: `net.ipv4.ip_unprivileged_port_start=80`** in `/etc/sysctl.d/`. One file that survives package upgrades, for IPv4 and IPv6 alike, and nothing else changes. Ports below 80 (SSH, SMTP, DNS) stay privileged. On a single-login server the only other processes are system daemons, and none of them can read Caddy's key (exception X15).
   - **B: the floor stays at 1024, and the host redirects 80 and 443** (TCP, and UDP for HTTP/3) to 8080 and 8443. The production overlay then publishes those, from `PUBLISHED_HTTP_PORT` and `PUBLISHED_HTTPS_PORT` in `.env`; empty, it publishes 80 and 443. Caddy still listens on 80 and 443 in its container, so links, redirects and `Alt-Svc` keep them.
     - The redirect is an nftables table of its own, loaded and removed by a unit of its own. Debian's `nftables.service` is never used for it: its stop runs `nft flush ruleset`, ufw's rules included.
     - ufw sees the port after the redirect, so it allows 8080 and 8443.
     - Measured in M12.5 ([deploy-rehearsal.md](../deploy-rehearsal.md#q74-way-b-measured)): the visitor's address is kept over TCP and HTTP/3, and a 10 MiB upload took the same time through the redirect as straight to 8443.
   - **What B does not buy:** 8080 and 8443 are open to any account, as 80 and 443 are under A. A local process that takes the port while the daemon has let it go receives the visitors either way. B keeps privileged only the ports from 81 to 1023, which nothing on the server uses. It suits a host whose policy keeps the kernel's default, or one where other people have accounts.
   - **Either way, `deploy.sh` never depends on the redirect, and refuses a host on neither way before the gates.** Its check from outside asks the published port (`PUBLISHED_HTTPS_PORT`, or 443). At step 3, under a rootless daemon, it refuses a published port below the floor and names both ways (`ops/checks.py published-ports`). RootlessKit would refuse that port at step 4, behind the gates, with an error that does not say why.
   - **Not used: `setcap cap_net_bind_service=ep` on `rootlesskit`.** It is narrower, and Docker's documentation offers it first. But an upgrade of `docker-ce-rootless-extras` replaces the binary and drops the capability, so 443 would stop answering at the next restart of the daemon.
4. **Network and port driver (Q75):** `slirp4netns` for both, set in `~/.config/systemd/user/docker.service.d/override.conf` (`DOCKERD_ROOTLESS_ROOTLESSKIT_NET`, `DOCKERD_ROOTLESS_ROOTLESSKIT_PORT_DRIVER`). The pilot host shows that the visitor's address reaches Caddy this way. allauth's per-address rate limits, the audit log and the session list need that address.
   - Without the override, `dockerd-rootless.sh` picks the `builtin` port driver. Before RootlessKit 3.0 that driver shows every visitor as one internal address, so one attacker's failed sign-ins would lock everyone out. From 3.0 it keeps the address for TCP only, and only with Docker's `userland-proxy` turned off. It never does for UDP, so HTTP/3 visitors would still share one address.
   - The accepted port driver is `slirp4netns` alone. Anything else is refused by `deploy.sh` (M12.3), `builtin` included, until a measurement changes this record.
   - **Measured in M12.5** on a daemon set up by the runbook ([deploy-rehearsal.md](../deploy-rehearsal.md#q75-the-port-driver-measured)): `slirp4netns` and `implicit` (with the `pasta` network driver) both showed the visitor's own address over TCP and HTTP/3; `builtin`, with Docker's defaults, showed the network's gateway over both. A 10 MiB upload through Caddy took 0.07 s with `slirp4netns`, 0.11 s with `pasta`, 0.06 s under the root daemon. `pasta` publishes a port only on every host address, never on one, and Docker calls it experimental. **The maintainer refused `implicit`** on 2026-11-25 (it was accepted until then): `deploy.sh` refuses it too, saying that it keeps the address but is not the chosen driver.
   - The driver in use must carry UDP, or the production overlay stops publishing 443/udp.
5. **Detection, not configuration.** The scripts decide that the daemon is rootless from what it says, never from a setting that a person could forget or get wrong. `docker info` asks the very daemon the scripts are about to use; its security options name `rootless`. A daemon that names `userns` (a root daemon with `userns-remap`) is not supported, and `deploy.sh` refuses it. A daemon that names neither is a root daemon, and every script behaves as it does today. If `docker info` fails, the scripts stop.
6. **One helper reads and writes under `backups/`** (M12.2, in `ops/common.sh`). Under a rootless daemon it runs one of a fixed set of operations inside the daemon's user namespace, where `deploy` is root and owns every mapped uid. It enters that namespace with `nsenter -U --preserve-credentials`: the user namespace only, so paths keep the host's meaning. The rules:
   - **The target is verified every time before it is entered.** It is the pid in `$XDG_RUNTIME_DIR/docker.pid`, and the CLI must talk to the socket next to it. That pid is `dockerd`'s, not `rootlesskit`'s. The process is `deploy`'s own, runs inside a user namespace that is not the caller's, and has `rootlesskit` as its parent. Its `uid_map` and `gid_map` map 0 to `deploy` alone, map the containers' uid and gid 1000, and map nothing outside `deploy`'s subordinate ranges.
   - **It fails closed:** any check fails, or `nsenter` is missing, and the script stops and says why.
   - **What it never does:**
     - it never falls back to reading as `deploy`;
     - it never widens a mode or an owner (`0777` stays CI only, `init.sh --ci`);
     - it never changes a dump's owner to `deploy`.
   - **Names are checked first.** A dump or snapshot name reaches the helper only after it matches its pattern. It arrives as its own argument, never inside a string that a shell parses again.

   Under a root daemon the helper runs the same operation directly, as `deploy`. [`docs/rootless-host.md`](../rootless-host.md) lists every line it replaces and the operations it allows.
7. **Ownership is the containers' uid, seen from inside.** `backups/` and everything in it belong to the containers' uid 1000, mode 0700 for directories and 0600 for files:
   - under a root daemon that is `deploy` (uid 1000, as the runbook requires);
   - under a rootless daemon it is host uid `subuid_start + 999` (100999 with the usual range). `init.sh` creates the directory that way through the helper, and gives what is already in it to that uid too.

   `deploy.sh` refuses, before anything changes, a `backups/` that is neither (or that is world-writable outside a CI runner's root daemon): the pre-deploy dump would fail with the site closed.

   `flags/` stays `deploy`'s, mode 0755, with world-readable flags. Its directory mode is what lets a shifted worker see `worker.paused`. The scripts create a missing one with that mode whatever their umask, and refuse one that others cannot search before they write the first flag. They never widen its mode themselves. The worker counts a pause flag it cannot check as set: only "no such file" means not paused, so a `flags/` it may not search keeps it paused and its log says why, each time it cannot check again (kept by the maintainer at the review).

## Alternatives rejected

- **The `docker` group:** root-equivalent. Docker says so, and that is what this record removes.
- **`sudo docker` in the scripts:** root at every call, and `sudo` asks for a password, so a script either prompts mid-deploy or gets a passwordless rule for `docker`, which is the `docker` group again.
- **`userns-remap` on a root daemon:** the containers are shifted, but the daemon is still root, and whoever holds its socket still is too.
- **Podman:** outside the stack (plan §2). The compose files, the scripts, the rehearsal and CI all speak to Docker Engine.
- **Making the dumps readable by `deploy`** (a group, an ACL, a `chown` after each dump): it widens who can read personal data on the host. Changing the owner of a shifted file needs the namespace anyway.
- **Reading `backups/` through a throwaway container:** it works under both daemons, but each read becomes a container start, with an image and its own uid and mounts. Reads that need the host's `python3` (`ops/checks.py`) would need it in the image too. Entering the namespace is one fixed command with checks that can be tested.

## Consequences

- **Shifted uids on bind mounts.** A file that a container writes to `backups/` belongs on the host to a uid that `deploy` cannot read without the helper. That includes the monthly `ls -l backups/.last-success`, which becomes `docker compose exec backup`. Files that `deploy` writes appear as root inside a container. `flags/` and the other bind-mounted files are world-readable, so they are unaffected.
- **The firewall governs published ports.** RootlessKit listens on the host like any process, so ufw sees the traffic (INPUT chain), unlike a root daemon's DNAT. The runbook allows only 22/tcp, 80/tcp, 443/tcp and 443/udp; with way B of point 3, 8080/tcp, 8443/tcp and 8443/udp instead of 80 and 443. A port is published once (`0.0.0.0:443`), not once per address: who reaches it is the firewall's decision.
- **The AppArmor profile is lost.** Docker's `docker-default` profile does not apply to rootless containers. seccomp, `cap_drop: ALL`, `no-new-privileges`, the read-only root filesystem and the user namespace still apply. `docs/security.md` records this.
- **User-mode networking is slower.** `slirp4netns` carries every packet in a user process. M12.5 measures a 10 MiB upload. The offsite copy and SMTP leave the same way.
- **Subordinate ids and linger are prerequisites.** Without `/etc/subuid` and `/etc/subgid` entries the daemon does not start. Without linger it stops at logout and does not start at boot, and the stack with it.
- **Limits.** `mem_limit` and `pids_limit` need cgroup v2 and systemd's delegation of `memory` and `pids`. Debian 13 has both by default, and the stack sets no CPU limit.
- **Where things live.** The data root moves to `~deploy/.local/share/docker`, which disk monitoring must watch. The socket is `/run/user/<uid>/docker.sock`, and `DOCKER_HOST` or the `rootless` context must reach every session that runs the scripts.
- **A host on the root daemon moves by restoring.** Volumes do not move across daemons. Dump, set up rootless, restore (runbook §4.4, M12.4): `recover.sh restore-offsite` starts the stack itself behind both gates, so no deploy comes first. Running `init.sh` once the rootless daemon runs gives the dumps already in `backups/`, which were `deploy`'s, to the containers' shifted uid, so the `backup` container can verify and restore them.
- **The root daemon stays supported** everywhere it is used today (CI, the rehearsal, developers' machines): under it, every script behaves as before. [ADR-0012](0012-maintenance-gate-deploys-recovery.md) still holds. Gates, the lock, dumps and the offsite copy are unchanged; only the way the host reaches `backups/` changes.
