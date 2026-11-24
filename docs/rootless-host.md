# The rootless host: design and file-access list

The design behind [ADR-0033](adr/0033-rootless-docker-host.md) (Docker rootless on the host, no `docker` group): the facts it rests on, how the scripts tell the daemons apart, and every place where a host script reads or writes under `backups/` or `flags/`. This document is the specification for the helper and the preflight checks that follow it. It is written for whoever changes `init.sh`, `deploy.sh`, `recover.sh`, `restore-drill.sh` or `ops/`. The runbook for setting up such a host is [runbooks/vps-setup.md](runbooks/vps-setup.md).

The line numbers refer to the scripts as they were when this document was written (commit `aa06558`). The change that rewrites a line updates its row.

## The facts, checked

Checked on 2026-11-24 against Docker's rootless documentation ([overview](https://docs.docker.com/engine/security/rootless/), [UID/GID mapping](https://docs.docker.com/engine/security/rootless/uid-gid-mapping/), [tips](https://docs.docker.com/engine/security/rootless/tips/), [troubleshooting](https://docs.docker.com/engine/security/rootless/troubleshoot/)), RootlessKit's documentation ([port drivers](https://github.com/rootless-containers/rootlesskit/blob/master/docs/port.md), [API](https://github.com/rootless-containers/rootlesskit/blob/master/docs/api.md)) and Moby's `contrib/dockerd-rootless.sh`. "Pilot" means it was seen on the pilot host (Debian 13 arm64, 2026-11-15) and nowhere else yet; M12.5 repeats those on amd64.

| Fact | Source | Status |
| --- | --- | --- |
| Container uid 0 is the user who runs the daemon; container uid N ≥ 1 is host uid `subuid_start + N − 1` (uid 1000 → 100999 with `100000:65536`). Files the user owns appear as root inside a container | Docker, UID/GID mapping | confirmed |
| At least 65,536 subordinate uids and gids in `/etc/subuid` and `/etc/subgid`; `newuidmap`/`newgidmap` from `uidmap`; `docker-ce-rootless-extras` holds the setup tool | Docker, overview | confirmed |
| The root daemon is turned off with `systemctl disable --now docker.service docker.socket` and `/var/run/docker.sock` removed; otherwise the setup tool needs `--force` and both daemons run | Docker, overview | confirmed |
| The daemon starts at boot only with `loginctl enable-linger <user>`; `--memory` and `--pids-limit` need cgroup v2 and systemd, which delegate only `memory` and `pids` by default | Docker, tips | confirmed. The stack sets no CPU limit |
| `docker info` lists `rootless` among its security options (with `seccomp` and `cgroupns`) | Docker, overview | confirmed |
| Ports below 1024: `setcap cap_net_bind_service=ep $(which rootlesskit)` or the sysctl `net.ipv4.ip_unprivileged_port_start` | Docker, tips | confirmed; Q74 chooses the sysctl with `80` (ADR-0033) |
| `dockerd-rootless.sh` picks the network driver `slirp4netns` if installed (v0.4.0 or later), else `pasta`, `vpnkit`, `gvisor-tap-vsock`; the port driver is `implicit` with `pasta`, `builtin` with any other; both are set by `DOCKERD_ROOTLESS_ROOTLESSKIT_NET` and `…_PORT_DRIVER` in `~/.config/systemd/user/docker.service.d/override.conf` | Docker, troubleshooting; `dockerd-rootless.sh` | confirmed |
| `builtin` hides the visitor's address; `slirp4netns` keeps it | pilot | **refined.** `slirp4netns` and `implicit` keep it. `builtin` keeps it from RootlessKit 3.0 for **TCP only**, and only with `"userland-proxy": false` in `~/.config/docker/daemon.json`; UDP is never kept, so HTTP/3 visitors would share one address. `gvisor-tap-vsock` never keeps it |
| Connections from the host's own loopback reach the containers from `10.0.2.2` | pilot | to re-check in M12.5 |
| A port is published once (`0.0.0.0:443`), not once per host address; RootlessKit listens like any process, so the INPUT chain (ufw) sees the traffic | pilot | to re-check in M12.5 with ufw on |
| Docker's AppArmor profile does not apply; seccomp, dropped capabilities, `no-new-privileges` and the read-only root filesystem do | Docker, troubleshooting (AppArmor listed as unsupported) | confirmed |
| `$XDG_RUNTIME_DIR/docker.pid` holds **`dockerd`'s** pid, not `rootlesskit`'s; RootlessKit is started without `--pidns`, so it is a pid of the host. `dockerd` runs in RootlessKit's user, mount and network namespaces; Docker documents `nsenter -U --preserve-credentials -n -m -t $(cat $XDG_RUNTIME_DIR/docker.pid)` to enter them | Docker, troubleshooting; `dockerd-rootless.sh` | confirmed |
| RootlessKit's state directory is `$XDG_RUNTIME_DIR/dockerd-rootless` (`DOCKERD_ROOTLESS_ROOTLESSKIT_STATE_DIR`), with its REST API on `api.sock` there; `GET /v1/info` returns `networkDriver.driver`, `portDriver.driver` and `portDriver.protos` | RootlessKit, API; `dockerd-rootless.sh` | confirmed from the documentation; M12.3 reads it on a real daemon |
| `deploy.sh` works unchanged with `DOCKER_HOST=unix:///run/user/<uid>/docker.sock`; `recover.sh status` works; nothing that reads `backups/` from the host does | pilot | the file-access list below explains each |
| Ubuntu 24.04 restricts unprivileged user namespaces and relies on the AppArmor profile that the deb package ships for `rootlesskit` | Docker, troubleshooting | Debian 13 is the runbook's system; M12.4 states what it needs |

## Which daemon

The scripts ask the daemon they are about to use, through `docker info`, and never read a setting:

```bash
docker info --format '{{json .SecurityOptions}}'
# root:     ["name=apparmor","name=seccomp,profile=builtin","name=cgroupns"]
# rootless: ["name=seccomp,profile=builtin","name=rootless","name=cgroupns"]
```

| Security options name | Kind | What the scripts do |
| --- | --- | --- |
| `rootless` | rootless | the helper enters the namespace; preflight checks the port driver |
| `userns` | root daemon with `userns-remap` | refused: the dumps belong to a range that `deploy` cannot enter |
| neither | root daemon | as today |
| `docker info` fails | unknown | stop: nothing else would work either |

The answer is computed once per run and kept in a variable, because each `docker info` costs a round trip.

## Entering the namespace

Under a rootless daemon the helper runs its operation with:

```bash
nsenter -U --preserve-credentials -t "$pid" -- /usr/bin/<tool> <fixed arguments>
```

It enters only the user namespace (`-U`). The mount namespace is not entered, so `backups/` still means the checkout's directory. Inside, `deploy`'s uid is 0 and has every capability over the files of mapped uids, so the containers' uid 1000 is readable. With `--preserve-credentials` the process keeps `deploy`'s uid, which the namespace maps to 0.

Before it enters, it checks the target, and stops at the first failure with the reason:

1. `$XDG_RUNTIME_DIR` is set, and `$XDG_RUNTIME_DIR/docker.pid` exists, belongs to `deploy` and holds a number.
2. `/proc/<pid>` exists and belongs to `deploy`, and `/proc/<pid>/exe` resolves to a file named `dockerd`.
3. Its parent (`PPid` in `/proc/<pid>/status`) belongs to `deploy` and its `exe` resolves to a file named `rootlesskit`.
4. `/proc/<pid>/ns/user` differs from `/proc/self/ns/user`: it is another namespace, not the caller's.
5. `/proc/<pid>/uid_map` maps 0 to `deploy`'s uid (length 1), and the rest to a range that starts at `deploy`'s entry in `/etc/subuid`.
6. `nsenter` is on the path (`util-linux`).

The daemon the CLI talks to and the pid checked here are the same only if `DOCKER_HOST` (or the current context) is `unix://$XDG_RUNTIME_DIR/docker.sock`. A `tcp://` or `ssh://` endpoint is refused under a rootless daemon: the namespace would not be the daemon's.

The tool is named by its absolute path and the arguments are fixed per operation (below), so neither `PATH` nor a name can turn the helper into a different command.

## Who owns what

| Path | Written by | Root daemon (host uid) | Rootless daemon (host uid) | Mode |
| --- | --- | --- | --- | --- |
| `backups/` | `init.sh` | `deploy` = 1000 | `subuid_start + 999`, through the helper | 0700 (`--ci`: 0777) |
| `backups/gmat-*.dump`, `.manifest.json`, `.media/` and its files | the `backup` container (uid 1000) | 1000 | `subuid_start + 999` | 0600 files, 0700 directories |
| `backups/.backup.lock`, `.last-success`, `.last-verify-ok` | the `backup` container | 1000 | `subuid_start + 999` | 0600 |
| `backups/.offsite/` (`recover.sh restore-offsite`) | the `backup` container, restic | 1000 | `subuid_start + 999` | restic's |
| `backups/.offsite/` (`restore-drill.sh --snapshot`) | a one-off container, `--user $(id -u):$(id -g)` | the operator (1000 by assumption) | container uid 1000, so `subuid_start + 999`; not "the operator" | restic's |
| `backups/<base>.*` (`restore-drill.sh --from-dir`) | `cp -a` on the host | the operator | cannot be written today | as copied |
| `flags/` | `init.sh`; `gates_on` if missing | `deploy` | `deploy` (container root) | 0755 |
| `flags/maintenance.on`, `flags/worker.paused` | `gates_on`; a person with `touch` | `deploy` | `deploy` | 0644 by `gates_on`; whatever the person's umask gives |

Read inside the containers: Caddy reads `flags/` as container root (the host's root under a root daemon, `deploy` under a rootless one). The worker reads it as uid 1000 (`deploy` under a root daemon, a shifted uid under a rootless one), so under a rootless daemon it sees the flags only through the directory's "other" bits. A flag file's own mode does not matter: checking that a file exists needs only search permission on its directory.

What the host scripts keep for themselves is not bind-mounted and does not change: `deploy.state`, `deployed.log`, `incident.log`, `deploy.head`, `.release/`, `preserved/`, `drill/`, the env files, and the lock (`/var/lock/gmat-m8-deploy`, in the world-writable `/run/lock` on Debian). The other bind mounts (`caddy/`, `clamav/*.conf`, `docker/postgres/initdb`) are made world-readable by `init.sh` and are read, never written.

## The file-access list

Every host-side read or write under `backups/` and `flags/`. "Rootless today" says what happens there now. "Change" is what M12.2 (or M12.3, where marked) does; "none" means the line works under both daemons.

### `init.sh`

| Line | Access | Rootless today | Change |
| --- | --- | --- | --- |
| 161 `mkdir -p backups flags` | creates both as `deploy` | `backups/` belongs to container root, so the `backup` container (uid 1000) cannot write to it, and every dump fails | `backups/`: create, then helper `prepare` |
| 162 `chmod 0700 backups` | mode | as above | inside `prepare` |
| 163 `chmod 0755 flags` | mode | works | none |
| 170 `chmod 0777 backups` (`--ci`) | mode | CI never runs rootless | none; never under a rootless daemon |

`init.sh` does not talk to the daemon today. Under the new design it asks `docker info` when it creates or checks `backups/`. If the daemon does not answer, it keeps today's behaviour, which widens nothing, and says that a rootless host must run it again once its daemon runs. `deploy.sh`'s preflight check below catches a host that did not.

### `ops/common.sh`

| Line | Access | Rootless today | Change |
| --- | --- | --- | --- |
| 90 `mkdir -p flags` (`gates_on`) | creates `flags/` if missing, under the scripts' `umask 077`, so mode 0700 | **fails open**: a shifted worker cannot search a 0700 directory, `Path.exists()` answers `False`, and the worker runs behind a closed gate | `install -d -m 0755 flags`, and refuse a `flags/` whose mode does not let others search it |
| 91–93 `: >` and `chmod 0644` of both flags | write | works | none |
| 163, 168 `rm -f` of each flag (`commit_point`) | write | works | none |
| 189 `[ -e ]` of both flags (`reached_step`) | read | works | none |
| 315–327 `restore_database`, 324 `<"$1"` | reads the dump into `pg_restore`'s stdin | **fails**: permission denied | the helper's `read dump BASE` on a pipe; the function takes the base name, not a path |
| 332 `[ -d "backups/$1.media" ]` (`restore_media`) | test | **silently skips the attachments**: the test is false, the function returns 0, and the restore goes on without them | the helper's `exists media BASE`; a test that cannot be answered stops the script, never reads as "no media" |
| 333 `-v "$PWD/backups/$1.media:/restored-media:ro"` | a bind mount, read by `web` as uid 1000 | works: the daemon mounts it and uid 1000 owns the files | none |
| 337–339 `verify_dump` | inside the `backup` container | works | none |

### `deploy.sh`

| Line | Access | Rootless today | Change |
| --- | --- | --- | --- |
| 99 `[ -e ]` of both flags (`refuse_unfinished`) | read | works | none |
| 206–209 `backup_before_migrating` | the dump's name from the container's output, checked only against `gmat-*` | works | the full dump pattern (below): `restore-predeploy` later uses that name as a path |
| (new, preflight, before the gates) | `backups/` stat | none today | M12.2: the helper's `stat`: `backups/` exists and belongs to the containers' uid 1000 as the namespace sees it (`deploy` under a root daemon), or has mode 0777 (CI); otherwise refuse with "run init.sh" before anything changes |

### `recover.sh`

| Line | Access | Rootless today | Change |
| --- | --- | --- | --- |
| 128–129 `[ -e ]` of both flags (`status`) | read | works | none; M12.3 adds the daemon's kind and port driver to `status` |
| 233–235 `[ -f "backups/$dump.dump" ]` (`restore-predeploy`) | test | **fails**: "not in backups/" | the dump pattern, then `exists dump BASE` |
| 243 `restore_database "backups/$dump.dump"` | read | fails | `restore_database BASE` (above) |
| 244 `restore_media` | as above | skips the attachments | as above |
| 288–289 `dump="${DUMP_ARG%.dump}"; dump="${dump##*/}"` (`restore`) | name taken from the command line | — | the dump pattern; anything else refused |
| 290–291 `[ -f ]` of the dump and its manifest | test | fails | `exists dump BASE`, `exists manifest BASE` |
| 292 `python3 ops/checks.py dump-version "backups/$dump.manifest.json"` | the host's `python3` reads the manifest | fails | `read manifest BASE \| python3 ops/checks.py dump-version -`: `checks.py` reads `-` as stdin, so `python3` never runs inside the namespace |
| 294 `checks.py dump-time` | read | fails | the same |
| 307, 308 `restore_database`, `restore_media` | read | as above | as above |
| 320–322 `fetch_snapshot`: restic in the `backup` container | writes `backups/.offsite/` as uid 1000 | works | the snapshot pattern before the call |
| 324 `find backups/.offsite/backups -maxdepth 1 -name 'gmat-*.dump'` | read | **fails**: "snapshot holds no dump" | the helper's `find-fetched`, then the dump pattern on its answer |
| 327 `mv backups/.offsite/backups/"$FETCHED"* backups/` | write | fails | the helper's `adopt BASE`: the three names `BASE.dump`, `BASE.manifest.json`, `BASE.media`, never a glob that also matches `BASE-other` |
| 328 `rm -rf backups/.offsite` | write | fails | the helper's `drop-offsite`: that one fixed path |
| 344, 345 `restore_database`, `restore_media` | read | as above | as above |

### `restore-drill.sh`

| Line | Access | Rootless today | Change |
| --- | --- | --- | --- |
| 81 `mkdir -p backups` | creates it as the operator | the `backup` container cannot write it (as in `init.sh`) | the helper's `prepare` |
| 83–87 restic in a one-off container, `--user "$(id -u):$(id -g)"` | writes `backups/.offsite/` | `id -u` is a container uid here, not the operator: 1000 becomes `subuid_start + 999` by coincidence | `--user 1000:1000` under both daemons, the `backup` service's own uid; the snapshot pattern before the call |
| 89 `find …` | read | fails | `find-fetched`, then the dump pattern |
| 92 `mv … "$DUMP"* backups/` | write | fails | `adopt BASE` |
| 93 `rm -rf backups/.offsite` | write | fails | `drop-offsite` |
| 95 `cp -a "$FROM_DIR/$DUMP".* backups/` | write | fails | the dump pattern on `--dump`, then the helper's `import DIR BASE`: copies the three names, then gives them to uid 1000 inside the namespace (the `backup` container reads them in `verify_dump`) |
| 97–98 `[ -f ]` of the dump and its manifest | test | fails | `exists dump BASE`, `exists manifest BASE` |
| 121 `checks.py compare-counts "backups/$DUMP.manifest.json" "$restored"` | read | fails | `read manifest BASE \| python3 ops/checks.py compare-counts - "$restored"` |
| 162 `<"backups/$DUMP.manifest.json"` (`check_attachments`) | read | fails | `read manifest BASE` on a pipe |
| 223, 224 `restore_database`, `restore_media` | read | as above | as above |
| 258 `rm -rf backups/gmat-* drill flags/maintenance.on flags/worker.paused` (`--wipe`) | write | `backups/gmat-*` fails; the rest works | the helper's `wipe-dumps` for `backups/`; `drill/` and the flags as today |

### `ops/checks.py`

| Function | Access | Change |
| --- | --- | --- |
| `_load` (line 49), for `dump-version`, `dump-time` and `compare-counts`' manifest | opens the path it is given | accept `-` for stdin. The host's `python3` then never needs to read `backups/` |

### Read by people, in the runbooks (M12.4)

`ls -l backups/.last-success backups/.last-verify-ok` (backup-restore.md, pilot.md, runbooks README, vps-setup.md §9) and `ls backups/` (deploy-rollback.md) fail on a rootless host. They become `docker compose exec backup ls -l /backups/…` or `restore --list`, which work under both daemons. `ls -ld backups flags` in vps-setup.md §8 expects `deploy` as the owner, and changes with the table above. `touch flags/…` (incident.md, email.md, the stack's README) works under both daemons.

## The helper's operations

One function in `ops/common.sh`, `backups_do OPERATION ARGUMENTS…`. Each operation is a fixed command line. Its only variable parts are a base name that already matched the dump pattern, or a directory given by the drill's operator. Each part is passed as its own argument:

| Operation | Command (inside the namespace under a rootless daemon, directly under a root one) |
| --- | --- |
| `prepare` | `mkdir -p backups`, then `chown 1000:1000 backups` (rootless only) and `chmod 0700 backups` |
| `stat` | `stat -c '%u %a' backups` |
| `exists dump\|manifest\|media BASE` | `test -f backups/BASE.dump`, `test -f backups/BASE.manifest.json`, `test -d backups/BASE.media`; exit 0 yes, 1 no, anything else stops the script |
| `read dump\|manifest BASE` | `cat -- backups/BASE.dump` or `backups/BASE.manifest.json` to stdout |
| `find-fetched` | `find backups/.offsite/backups -maxdepth 1 -type f -name 'gmat-*.dump' -print -quit` |
| `adopt BASE` | `mv --` each of `backups/.offsite/backups/BASE.{dump,manifest.json,media}` that exists into `backups/` |
| `drop-offsite` | `rm -rf -- backups/.offsite` |
| `import DIR BASE` | `cp -a --` each of `DIR/BASE.{dump,manifest.json,media}` that exists into `backups/`, then `chown -R 1000:1000` those three (rootless only) |
| `wipe-dumps` | `find backups -maxdepth 1 -name 'gmat-*' -exec rm -rf -- {} +` |

Tools are called by absolute path. No operation writes outside `backups/`. None changes a mode or an owner except `prepare` and `import`, which set uid 1000 and 0700, the owner and mode that a dump has anyway. Nothing is ever given to `deploy`.

## Names

A name is checked before it reaches the helper, a path or a container:

| Name | Pattern | Where it comes from |
| --- | --- | --- |
| A dump's base | `^gmat-[0-9]{8}T[0-9]{6}Z(-[A-Za-z0-9._-]{1,40})?$` (`backup.sh`: the stamp and its `--label` rule) | `deploy.sh` (the backup's output), `recover.sh restore DUMP`, `--dump`, `deploy.state`, and the answer of `find-fetched` |
| A restic snapshot | `^[0-9a-f]{8,64}$` | `recover.sh --snapshot`, `restore-drill.sh --snapshot` |

Before this change only `deploy.sh` checked a dump's name (`gmat-*`), and nothing checked a snapshot id, the `restore` argument or `--dump`. The patterns apply under both daemons.

## The port driver (M12.3)

Under a rootless daemon, `deploy.sh`'s preflight reads the port driver in this order and uses the first answer:

1. RootlessKit's API: `curl --silent --max-time 5 --unix-socket "$XDG_RUNTIME_DIR/dockerd-rootless/api.sock" http://rootlesskit/v1/info`, then `portDriver.driver` and `portDriver.protos` (parsed by `ops/checks.py`). This is what the running daemon uses.
2. The `--port-driver=` argument on the command line of the `rootlesskit` process found above (`/proc/<ppid>/cmdline`, NUL-separated). `dockerd-rootless.sh` always passes it.
3. Neither: warn, and point at the runbook's check (the address in `/account/sessions/` seen from another machine).

`slirp4netns` and `implicit` pass. Any other driver is refused, `builtin` included (ADR-0033 point 4), with the fix: the override file and `systemctl --user restart docker`. A driver whose `protos` lacks `udp` is also refused, because the production overlay publishes 443/udp. `systemctl --user show docker -p Environment` is not a signal: it shows what the unit would set, not what runs, and an empty value means `builtin`.

## Found while writing this

These go beyond the step cards. Each is handled above, and the maintainer sees them in the pull request:

- **The M12.2 card's target check was wrong.** It says to refuse a pid whose `/proc/<pid>/exe` is not `rootlesskit`, but `docker.pid` holds `dockerd`'s pid. The check above verifies `dockerd` and its `rootlesskit` parent.
- **The M12.2 card says the names are "already validated".** They are not, except the pre-deploy dump's, loosely. The patterns above are new.
- **`restore_media` skips the attachments silently** on a rootless host. A restore would report success without them.
- **`gates_on` fails open** if `flags/` is missing on a rootless host (0700 under `umask 077`). The worker also reads an unreadable directory as "not paused". The script fix is above. Making the worker treat any error other than "no such file" as paused, and log it, is a small change in `apps/notifications/worker.py`. It is proposed for M12.2, where the maintainer can accept it or leave it out.
- **The drill's `--user "$(id -u):$(id -g)"`** names a container uid, which is not the operator under a rootless daemon.
- **Q75's premise changed.** `builtin` keeps the address from RootlessKit 3.0 for TCP only. That still does not cover HTTP/3, so the decision holds, for a different reason.
