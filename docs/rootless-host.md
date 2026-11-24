# The rootless host: design and file-access list

The design behind [ADR-0033](adr/0033-rootless-docker-host.md) (Docker rootless on the host, no `docker` group): the facts it rests on, how the scripts tell the daemons apart, and every place where a host script reads or writes under `backups/` or `flags/`. This document is the specification for the helper and the preflight checks that follow it. It is written for whoever changes `init.sh`, `deploy.sh`, `recover.sh`, `restore-drill.sh` or `ops/`. The runbook for setting up such a host is [runbooks/vps-setup.md](runbooks/vps-setup.md).

The line numbers refer to the scripts as they were when this document was written (commit `aa06558`); each row says what its line became. A change that rewrites one of these lines updates its row.

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
| RootlessKit's state directory is `$XDG_RUNTIME_DIR/dockerd-rootless` (`DOCKERD_ROOTLESS_ROOTLESSKIT_STATE_DIR`), with its REST API on `api.sock` there; `GET /v1/info` returns `networkDriver.driver`, `portDriver.driver` and `portDriver.protos` | RootlessKit, API; `dockerd-rootless.sh` | confirmed on a real daemon (M12.3, below) |
| `deploy.sh` works unchanged with `DOCKER_HOST=unix:///run/user/<uid>/docker.sock`; `recover.sh status` works; nothing that reads `backups/` from the host does | pilot | the file-access list below explains each |
| Ubuntu 24.04 restricts unprivileged user namespaces and relies on the AppArmor profile that the deb package ships for `rootlesskit` | Docker, troubleshooting | Debian 13 is the runbook's system and its defaults are enough (vps-setup.md §4.1); the runbook names no Ubuntu step |

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

1. `$XDG_RUNTIME_DIR` is set, and the CLI talks to the socket in it (below).
2. `$XDG_RUNTIME_DIR/docker.pid` exists, belongs to `deploy` and holds a number.
3. `/proc/<pid>` exists and belongs to `deploy`, and `/proc/<pid>/exe` resolves to a file named `dockerd`. A binary replaced since the daemon started (`… (deleted)`, after a package upgrade) still counts: the process was started from it.
4. Its parent (`PPid` in `/proc/<pid>/status`) belongs to `deploy` and its `exe` resolves to a file named `rootlesskit`. That parent is RootlessKit's child process, which runs inside the namespaces as `/proc/self/exe`; its `exe` link names the `rootlesskit` binary.
5. `/proc/<pid>/ns/user` differs from `/proc/self/ns/user`: it is another namespace, not the caller's. A shell that already entered it by hand is refused.
6. `/proc/<pid>/uid_map` maps 0 to `deploy`'s uid alone (length 1), maps the containers' uid 1000, and maps nothing outside `deploy`'s entries in `/etc/subuid`, by name or uid. `gid_map` is checked the same way against `/etc/subgid` and the containers' gid 1000.
7. `nsenter` is on the path (`util-linux`).

The daemon the CLI talks to and the pid checked here are the same only if `DOCKER_HOST` (or, without it, the current context) is `unix://$XDG_RUNTIME_DIR/docker.sock`, compared after resolving both paths. A `tcp://` or `ssh://` endpoint is refused under a rootless daemon: the namespace would not be the daemon's.

The checks run before every operation, not once per script, so a daemon restarted in between is checked again. `DAEMON_KIND` and `NAMESPACE_PID` are cleared when `ops/common.sh` is sourced, so the environment cannot preset either.

The tool is named by its absolute path and the arguments are fixed per operation (below), so neither `PATH` nor a name can turn the helper into a different command. `nsenter` itself comes from `PATH`, like `docker` and `cosign`.

This was tried on the maintainer's Linux machine with an unprivileged user namespace (`unshare -U -r`): `nsenter -U --preserve-credentials -t <pid> -- /usr/bin/stat` reports the account's own files as uid 0, and `id -u` inside answers 0. The fake process tree of the unit tests (`tests/unit/docker/conftest.py`) covers each check; M12.5 runs the scripts against a real rootless daemon.

## Who owns what

| Path | Written by | Root daemon (host uid) | Rootless daemon (host uid) | Mode |
| --- | --- | --- | --- | --- |
| `backups/` | `init.sh` | `deploy` = 1000 | `subuid_start + 999`, through the helper | 0700 (`--ci`: 0777, root daemon only) |
| `backups/gmat-*.dump`, `.manifest.json`, `.media/` and its files | the `backup` container (uid 1000) | 1000 | `subuid_start + 999` | 0600 files, 0700 directories |
| `backups/.backup.lock`, `.last-success`, `.last-verify-ok` | the `backup` container | 1000 | `subuid_start + 999` | 0600 |
| `backups/.offsite/` (`recover.sh restore-offsite`) | the `backup` container, restic | 1000 | `subuid_start + 999` | restic's |
| `backups/.offsite/` (`restore-drill.sh --snapshot`) | a one-off container, `--user 1000:1000` (until M12.2: the operator's `$(id -u):$(id -g)`) | 1000 | `subuid_start + 999` | restic's |
| `backups/<base>.{dump,manifest.json,media}` (`restore-drill.sh --from-dir`) | the helper's `import` | the operator (`deploy`, 1000) | `subuid_start + 999` | as copied |
| `flags/` | `init.sh`; `gates_on` if missing | `deploy` | `deploy` (container root) | 0755 |
| `flags/maintenance.on`, `flags/worker.paused` | `gates_on`; a person with `touch` | `deploy` | `deploy` | 0644 by `gates_on`; whatever the person's umask gives |

Read inside the containers: Caddy reads `flags/` as container root (the host's root under a root daemon, `deploy` under a rootless one). The worker reads it as uid 1000 (`deploy` under a root daemon, a shifted uid under a rootless one), so under a rootless daemon it sees the flags only through the directory's "other" bits. A flag file's own mode does not matter: checking that a file exists needs only search permission on its directory.

What the host scripts keep for themselves is not bind-mounted and does not change: `deploy.state`, `deployed.log`, `incident.log`, `deploy.head`, `.release/`, `preserved/`, `drill/`, the env files, and the lock (`/var/lock/gmat-m8-deploy`, in the world-writable `/run/lock` on Debian). The other bind mounts (`caddy/`, `clamav/*.conf`, `docker/postgres/initdb`) are made world-readable by `init.sh` and are read, never written.

## The file-access list

Every host-side read or write under `backups/` and `flags/`, as M12.1 listed them, with the line numbers of commit `aa06558`. "Rootless before" is what happened there before M12.2. "Now" is what the line became in M12.2 (or M12.3, where marked): an operation of the helper, or "unchanged" where the line works under both daemons.

### `init.sh`

| Line | Access | Rootless before | Now |
| --- | --- | --- | --- |
| 161 `mkdir -p backups flags` | creates both as `deploy` | `backups/` belonged to container root, so the `backup` container (uid 1000) could not write to it, and every dump failed | `flags/` unchanged; `backups/` through `prepare` |
| 162 `chmod 0700 backups` | mode | as above | inside `prepare` |
| 163 `chmod 0755 flags` | mode | worked | unchanged |
| 170 `chmod 0777 backups` (`--ci`) | mode | CI never runs rootless | unchanged on a root daemon; `--ci` on a rootless daemon is refused |

`init.sh` sources `ops/common.sh` for the helper and asks `docker info`. If the daemon does not answer, it creates `backups/` as before (as `deploy`, mode 0700), which widens nothing, and says that a rootless host must run it again once its daemon runs. `deploy.sh`'s preflight refuses a host that did not. A daemon that remaps users is refused.

### `ops/common.sh`

| Line | Access | Rootless before | Now |
| --- | --- | --- | --- |
| 90 `mkdir -p flags` (`gates_on`) | creates `flags/` if missing, under the scripts' `umask 077`, so mode 0700 | **failed open**: a shifted worker cannot search a 0700 directory, `Path.exists()` answers `False`, and the worker ran behind a closed gate | `require_flags_searchable` refuses a `flags/` whose mode does not let others search it, before the first flag is written, so the gates stay as they were; a missing one is created with `mkdir -m 0755` |
| 91–93 `: >` and `chmod 0644` of both flags | write | worked | unchanged |
| 163, 168 `rm -f` of each flag (`commit_point`) | write | worked | unchanged |
| 189 `[ -e ]` of both flags (`reached_step`) | read | worked | unchanged |
| 315–327 `restore_database`, 324 `<"$1"` | reads the dump into `pg_restore`'s stdin | **failed**: permission denied | `restore_database BASE`: `exists dump` before the database is dropped, then `read dump` on a pipe |
| 332 `[ -d "backups/$1.media" ]` (`restore_media`) | test | **skipped the attachments silently** | `exists media`: not there means no media; a link, or a test that cannot be answered, stops the restore |
| 333 `-v "$PWD/backups/$1.media:/restored-media:ro"` | a bind mount, read by `web` as uid 1000 | worked | unchanged |
| 337–339 `verify_dump` | inside the `backup` container | worked | unchanged |

### `deploy.sh`

| Line | Access | Rootless before | Now |
| --- | --- | --- | --- |
| 99 `[ -e ]` of both flags (`refuse_unfinished`) | read | worked | unchanged |
| 206–209 `backup_before_migrating` | the dump's name from the container's output, checked only against `gmat-*` | worked | the dump pattern (`dump_name_ok`): `restore-predeploy` later reads `backups/` under that name |
| new: `check_host_directories`, after the env files and before the release is fetched | `flags/` mode, `backups/` stat | — | `require_flags_searchable`, then the helper's `stat`: `backups/` is uid 1000 mode 700 as the helper sees it (`deploy` under a root daemon), or mode 777 on a root daemon (a CI runner, `init.sh --ci`). Otherwise "run bash init.sh", before anything changes |

### `recover.sh`

| Line | Access | Rootless before | Now |
| --- | --- | --- | --- |
| 128–129 `[ -e ]` of both flags (`status`) | read | worked | unchanged; since M12.3 `status` also prints the daemon's kind and port driver, read-only |
| 233–235 `[ -f "backups/$dump.dump" ]` (`restore-predeploy`) | test | **failed**: "not in backups/" | the dump pattern on the recorded name, `backups_ready`, then `exists dump`, all before the confirmation |
| 243 `restore_database "backups/$dump.dump"` | read | failed | `restore_database BASE` (above) |
| 244 `restore_media` | as above | skipped the attachments | as above |
| 288–289 `dump="${DUMP_ARG%.dump}"; dump="${dump##*/}"` (`restore`) | name taken from the command line | — | the dump pattern on what is left; anything else refused |
| 290–291 `[ -f ]` of the dump and its manifest | test | failed | `backups_ready`, `exists dump`, `exists manifest` |
| 292 `python3 ops/checks.py dump-version "backups/$dump.manifest.json"` | the host's `python3` reads the manifest | failed | without `--version`: `read manifest BASE \| python3 ops/checks.py dump-version -`, so `python3` never runs inside the namespace |
| 294 `checks.py dump-time` | read | failed | `read manifest BASE \| python3 ops/checks.py dump-time -` |
| 307, 308 `restore_database`, `restore_media` | read | as above | as above |
| 320–322 `fetch_snapshot`: restic in the `backup` container | writes `backups/.offsite/` as uid 1000 | worked | the snapshot pattern and `backups_ready` before the lock and the gates |
| 324 `find backups/.offsite/backups -maxdepth 1 -name 'gmat-*.dump'` | read | **failed**: "snapshot holds no dump" | `find-fetched`, then the dump pattern on its answer |
| 327 `mv backups/.offsite/backups/"$FETCHED"* backups/` | write | failed | `adopt BASE`: the three names `BASE.dump`, `BASE.manifest.json`, `BASE.media`, never a glob that also matches `BASE-other` |
| 328 `rm -rf backups/.offsite` | write | failed | `drop-offsite`: that one fixed path |
| 344, 345 `restore_database`, `restore_media` | read | as above | as above |

### `restore-drill.sh`

| Line | Access | Rootless before | Now |
| --- | --- | --- | --- |
| 81 `mkdir -p backups` | creates it as the operator | the `backup` container could not write it (as in `init.sh`) | `prepare`, the drill's first change: it checks the namespace target before the gates |
| 83–87 restic in a one-off container, `--user "$(id -u):$(id -g)"` | writes `backups/.offsite/` | `id -u` is a container uid here, not the operator: 1000 became `subuid_start + 999` by coincidence | `--user 1000:1000` (`$CONTAINER_UID:$CONTAINER_GID`) under both daemons, the `backup` service's own uid; the snapshot pattern when the options are read |
| 89 `find …` | read | failed | `find-fetched`, then the dump pattern |
| 92 `mv … "$DUMP"* backups/` | write | failed | `adopt BASE` |
| 93 `rm -rf backups/.offsite` | write | failed | `drop-offsite` |
| 95 `cp -a "$FROM_DIR/$DUMP".* backups/` | write | failed | the dump pattern on `--dump` when the options are read, then `import DIR BASE`: copies the three names, then, under a rootless daemon, gives them to uid 1000 inside the namespace (the `backup` container reads them in `verify_dump`) |
| 97–98 `[ -f ]` of the dump and its manifest | test | failed | `exists dump`, `exists manifest` |
| 121 `checks.py compare-counts "backups/$DUMP.manifest.json" "$restored"` | read | failed | `read manifest BASE \| python3 ops/checks.py compare-counts - "$restored"` |
| 162 `<"backups/$DUMP.manifest.json"` (`check_attachments`) | read | failed | `read manifest BASE` on a pipe into the one-off `web` |
| 223, 224 `restore_database`, `restore_media` | read | as above | as above |
| 258 `rm -rf backups/gmat-* drill flags/maintenance.on flags/worker.paused` (`--wipe`) | write | `backups/gmat-*` failed; the rest worked | `wipe-dumps` for `backups/`; `drill/` and the flags unchanged |

`--preserved-dump FILE` is still read by the host (`<"$1"`): it is a file the operator fetched by hand, outside `backups/`.

### `ops/checks.py`

| Function | Access | Now |
| --- | --- | --- |
| `_load` (line 49), for `dump-version`, `dump-time` and `compare-counts`' manifest | opens the path it is given | `-` reads stdin. The host's `python3` never reads `backups/` |

### Read by people, in the runbooks (M12.4)

`ls -l backups/.last-success backups/.last-verify-ok` (backup-restore.md, pilot.md, runbooks README, vps-setup.md §9) and `ls backups/` (deploy-rollback.md) fail on a rootless host. They are now `docker compose exec backup ls -l /backups/…` and `restore --list`, which work under both daemons. `ls -ld backups flags` in vps-setup.md §8 expected `deploy` as the owner: it now checks `flags/` as `deploy`'s and `backups/` as the containers' uid, shown as a number. The restore drill's wipe check (`ls backups`) and an incident's copy of a dump (`dc cp backup:/backups/…`, through the daemon) follow the same rule. `touch flags/…` (incident.md, email.md, the stack's README) works under both daemons. A restore on a new rootless server needs `bash init.sh` after the env files are in place, because `backups_ready` stops on a `backups/` that does not exist (backup-restore.md, "The VPS is gone").

## The helper's operations

One function in `ops/common.sh`, `backups_do OPERATION ARGUMENTS…`. Each operation is a fixed command line. Its only variable parts are a dump's base name, checked against the dump pattern again inside the helper whatever the caller did, and a directory given by the drill's operator. Each part is passed as its own argument, and each operation takes a fixed number of them. `backups` below is the absolute path of the stack's `backups/` directory, and every tool is `/usr/bin/<tool>`:

| Operation | Command (inside the namespace under a rootless daemon, directly under a root one) |
| --- | --- |
| `prepare` | `mkdir -p -- backups`; under a rootless daemon only, `chown -R --no-dereference 1000:1000 -- backups`; `chmod 0700 -- backups` |
| `stat` | `stat -c '%u %a' -- backups` |
| `exists dump\|manifest\|media BASE` | `find backups -mindepth 1 -maxdepth 1 -name BASE.dump -printf %y` (or `BASE.manifest.json`, `BASE.media`). Nothing printed: 1, not there. `f` (`d` for the media): 0. Any other type, a link say, or a failure stops the script; it never reads as "not there" |
| `read dump\|manifest BASE` | `cat -- backups/BASE.dump` or `backups/BASE.manifest.json` to stdout |
| `find-fetched` | `find backups/.offsite/backups -mindepth 1 -maxdepth 1 -type f -name 'gmat-*.dump' -print -quit` |
| `adopt BASE` | `find backups/.offsite/backups -mindepth 1 -maxdepth 1 ( -name BASE.dump -o -name BASE.manifest.json -o -name BASE.media ) -exec mv -t backups -- {} +` |
| `drop-offsite` | `rm -rf -- backups/.offsite` |
| `import DIR BASE` | the same `find` in `DIR` (made absolute) with `-exec cp -a -t backups -- {} +`; then, under a rootless daemon, the same `find` in `backups` with `-exec chown -R --no-dereference 1000:1000 -- {} +` |
| `wipe-dumps` | `find backups -mindepth 1 -maxdepth 1 -name 'gmat-*' -exec rm -rf -- {} +` |

No operation writes outside `backups/`. None changes a mode or an owner except `prepare` and `import`, which set uid 1000 and 0700, the owner and mode that a dump has anyway. Under a rootless daemon, `prepare` gives everything already in `backups/` to uid 1000. A host that ran the root daemon left its dumps to `deploy`, which is root inside the namespace, so this keeps them readable to the `backup` container after the move. Nothing is ever given to `deploy`. `find` never follows a link (`-P`, its default), and `chown` never changes what a link points to.

`backups_ready` (`detect_daemon`, then `stat`) runs before anything changes in `recover.sh restore`, `restore-predeploy` and `restore-offsite`. A refusal of the target checks there leaves both gates as the script found them. The drill's first change is `prepare`, before its gates. `deploy.sh` reads only `stat`.

## Names

A name is checked before it reaches the helper, a path or a container, in the C locale, so that `[0-9]` takes in no digit of another script:

| Name | Pattern | Where it comes from |
| --- | --- | --- |
| A dump's base | `^gmat-[0-9]{8}T[0-9]{6}Z(-[A-Za-z0-9._-]{1,40})?$` (`backup.sh`: the stamp and its `--label` rule) | `deploy.sh` (the backup's output), `recover.sh restore DUMP`, `--dump`, `deploy.state`, and the answer of `find-fetched` |
| A restic snapshot | `^[0-9a-f]{8,64}$` | `recover.sh --snapshot`, `restore-drill.sh --snapshot` |

Until M12.2 only `deploy.sh` checked a dump's name (`gmat-*`), and nothing checked a snapshot id, the `restore` argument or `--dump`. The patterns apply under both daemons. A snapshot is named by its id, never `latest`: on a host whose offsite credentials can only append, an attacker can add snapshots, and the newest one is then theirs.

## The host checks of `deploy.sh` (M12.3)

`deploy.sh` checks the host itself after taking the lock and before it fetches a release, so a host set up the wrong way is refused before anything changes, and the message names the fix. Every check is read-only, and `--resume` runs them the same way: gates that an earlier failure left on stay on.

| Check | Refused | Warned |
| --- | --- | --- |
| `cosign version`'s `GitVersion` (`ops/checks.py cosign-version`) | a major below 3, or no version found: "cosign 3 or newer is required (vps-setup.md §4.3)". cosign 2 finds none of the signatures cosign 3 makes (M11) | — |
| `backups/` and `flags/` (`check_host_directories`, M12.2) | as the file-access list says | — |
| a rootless daemon's port driver (below) | one that hides the visitor's address or does not carry UDP | none of its sources names it |
| the `docker` group (`getent group docker`, `id -G`) | — | this account is a member, under either daemon: the group is root on the host, and the runbook's host leaves it empty. Never refused: CI runners and developers' machines use it |

### The port driver

Under a rootless daemon, after the namespace target checks pass (they find RootlessKit's pid), `port_driver` in `ops/common.sh` reads the driver in this order and uses the first answer that names one:

1. RootlessKit's API: `curl --silent --fail --max-time 5 --unix-socket "$XDG_RUNTIME_DIR/dockerd-rootless/api.sock" http://rootlesskit/v1/info`, then `portDriver.driver` and `portDriver.protos` (`ops/checks.py port-driver --from api`). This is what the running daemon uses. An answer that is not JSON or names no driver leaves it to the next source.
2. The `--port-driver` argument of the `rootlesskit` process that is `dockerd`'s parent (`/proc/<ppid>/cmdline`, NUL-separated; `--from cmdline`). RootlessKit's child runs with its parent's arguments, and `dockerd-rootless.sh` always passes one. Given more than once, the **last** counts, as RootlessKit reads it.
3. Neither: a warning that points at the runbook's check (the address in `/account/sessions/`, seen from another machine), and the deploy goes on.

`slirp4netns` and `implicit` pass. Any other driver is refused (ADR-0033 point 4), `builtin` included, with why (allauth's per-address rate limits would count every visitor as one, and the audit log's addresses would mean nothing) and the fix: the override file and `systemctl --user restart docker`. A driver whose `protos` lack `udp` is refused too, because the production overlay publishes 443/udp. `systemctl --user show docker -p Environment` is not a source: it shows what the unit would set, not what runs, and an empty value means `builtin`. `ops/checks.py port-driver` exits 0 (keeps the address), 2 (refused) or 3 (does not say).

`recover.sh status` prints the daemon's kind and, under a rootless daemon, the port driver as `deploy.sh` would judge it. It never stops on either: a daemon that does not answer, or a target that fails its checks, is shown as `unknown` with the reason.

Checked on 2026-11-24 against a real rootless `dockerd` (Docker's `docker:29-dind-rootless` image, Docker 29.10.0, RootlessKit 3.3.0, run privileged and thrown away on the maintainer's Linux machine). The answers and command lines are the fixtures of `tests/unit/docker/fixtures/`:

- With the default `builtin`, `/v1/info` lists `tcp`, `udp` and their 4/6 forms in `portDriver.protos`, and `extra.sourceIPTransparentBackend: iptables` (RootlessKit 3.0's TCP-only address keeping). `protos` alone would let it pass, so the driver's name decides first.
- With `slirp4netns`, `protos` is `tcp, tcp4, udp, udp4`, and `disallowLoopbackChildIP` is `true`.
- With `--port-driver=builtin` and a later `--port-driver=slirp4netns` on the command line, the API reported `slirp4netns`: the last one counts.
- RootlessKit's child process (`/proc/self/exe`) carries the same arguments as its parent.
- The `dockerd-rootless.sh` of `docker-ce-rootless-extras` 29.9.3 offers a new port driver, `pesto` (with `pasta`, experimental, IPv4 only). It is refused like any driver ADR-0033 does not name; a measurement may add it.

That image starts `rootlesskit` itself, without `dockerd-rootless.sh`, under `docker-init`, and with a state directory in `/tmp`, so it is not the runbook's process tree: M12.5 runs `deploy.sh` and `recover.sh status` against a daemon set up by the runbook.

## Found while writing this

These went beyond the step cards. Each is handled above, and the maintainer sees them in the pull request:

- **The M12.2 card's target check was wrong.** It says to refuse a pid whose `/proc/<pid>/exe` is not `rootlesskit`, but `docker.pid` holds `dockerd`'s pid. The helper checks `dockerd` and its `rootlesskit` parent (M12.2).
- **The M12.2 card says the names are "already validated".** They were not, except the pre-deploy dump's, loosely. The patterns above are new (M12.2).
- **`restore_media` skipped the attachments silently** on a rootless host: a restore would have reported success without them. `exists media` now tells "not there" from "cannot tell" (M12.2).
- **`gates_on` failed open** if `flags/` was missing on a rootless host (0700 under `umask 077`). The script fix is above (M12.2). The worker also read a pause flag it could not check as "not paused", because Python's `Path.exists()` answers `False` on a permission error. Since M12.2 it counts a flag it cannot check as set, logs why, and stays paused (`apps/notifications/worker.py`); the maintainer may still leave that change out.
- **The drill's `--user "$(id -u):$(id -g)"`** named a container uid, which is not the operator under a rootless daemon. It is uid 1000 under both daemons (M12.2).
- **A restore on a new rootless server needs `bash init.sh` first.** The runbook said "no `init.sh`" for the loss of a VPS (so that no secret is generated), but `backups_ready` stops on a `backups/` that does not exist. With both env files in place `init.sh` generates and overwrites nothing, and it creates `backups/` for the containers' uid. backup-restore.md says so (M12.4).
- **Q75's premise changed.** `builtin` keeps the address from RootlessKit 3.0 for TCP only. That still does not cover HTTP/3, so the decision holds, for a different reason.
