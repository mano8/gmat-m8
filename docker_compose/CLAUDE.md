# `docker_compose/` and `docker/`

The stack, its images and the host scripts. Loaded when you work in this directory; the rules that hold everywhere are in the root `CLAUDE.md`. The commands are in the root (Commands) and in `gmat_m8/README.md`.

## Rules in detail

The full text of the root `CLAUDE.md`'s rules this directory implements; the root keeps a one-sentence summary of each.

- **Containers:** non-root uid 1000 (the `decoder` uid 1001, `clamd`/`freshclam` their image's uid 100, `db` its image's uid 70, no capability, so `gosu` never runs), read-only root fs; only Caddy publishes ports; only Caddy and `web` share `edge`; each DB client (`web`, `worker`, `backup`, `migrate`) has its own internal network; DB never published; what parses uploads (`clamd`, `decoder`) has no network and no secret, and `web` reaches it through a unix socket on a volume only the two mount, read-only on `web`'s side (ADR-0015).
- **Rootless host (ADR-0033, M12):** production runs Docker rootless as `deploy`, no root daemon, nobody in the `docker` group; the root daemon stays supported everywhere else (CI, the rehearsal, developers) and every script behaves as before under it; the rehearsal passes under both (M12.5, `docs/deploy-rehearsal.md`). The scripts tell the daemons apart from `docker info`'s security options (`rootless`; `userns` refused), never from a setting. Under a rootless daemon the containers' files under `backups/` belong to a shifted uid: a host-side read or write there goes through the one helper of `ops/common.sh`, which enters the daemon's user namespace after checking its target, fails closed, and never widens a mode or gives a file to `deploy`; a dump or snapshot name matches its pattern first. `flags/` stays `deploy`'s, mode 0755, so a shifted worker sees the flags. Before changing a line that touches `backups/` or `flags/`, read [`docs/rootless-host.md`](../docs/rootless-host.md) (the facts, the file-access list, the helper's operations) and keep its row true. `deploy.sh` checks the host before it fetches a release (M12.3, read-only, `--resume` too): cosign 3 or newer, and under a rootless daemon a port driver that keeps the visitor's address (`slirp4netns` alone, `implicit` refused by the maintainer in M12.5; RootlessKit's API first, then its command line, a warning when neither says); a member of the `docker` group is warned, never refused; `recover.sh status` prints the daemon and the driver and never stops on them. Ports 80 and 443 (ADR-0033 point 3): the operator lowers the kernel's floor to 80 (way A, the default) or redirects 80 and 443 to the ports the production overlay then publishes (way B, `PUBLISHED_HTTP_PORT` and `PUBLISHED_HTTPS_PORT` in `.env`); at step 3 `deploy.sh` refuses a port the rootless daemon could not publish, and its check from outside asks the published port, so no script depends on a host's redirect.
- **Edge (ADR-0034, M13; implemented in M13.2–M13.4):**
  - **Settings.** `EDGE_TLS` (`acme`|`files`), `EDGE_EXTRA_HOST`, and `EDGE_PROXY` (`none`|`cloudflare`, with `EDGE_TUNNEL_SUBNET` and `EDGE_TUNNEL_ADDRESS`), all off by default. Without them, Caddy's configuration is what it was before they existed, apart from its two `header_up` lines.
  - **How Caddy picks a mode.** It imports `caddy/edge/<kind>-<value>.caddy`. Each file defines named snippets, so a value names exactly one file or Caddy refuses to start, a glob that matches no file or two files included. Compose passes `${EDGE_TLS:-acme}`, because a value that is set but empty does not take Caddy's `{$VAR:default}`. `ops/checks.py edge` refuses anything outside the allowlist first.
  - **Who is believed.** Only in `cloudflare` mode, only `CF-Connecting-IP`, and only from `{$EDGE_TUNNEL_ADDRESS}/32`. The literal suffix makes Caddy refuse a range, a list, a keyword or an empty value. That address is the `cloudflared` service's fixed `ipv4_address` on `tunnel`, which only Caddy and `cloudflared` join, set by the same key. Never `X-Forwarded-For`, a subnet, IPv6 or the host's loopback: under the rootless daemon `10.0.2.2` is every local process.
  - **What Django reads.** Both proxies set `header_up X-Forwarded-For {client_ip}` and `header_up -Cf-Connecting-Ip` in every mode.
  - **`cloudflare` mode** also means:
    - a deferred `Cache-Control: no-transform`, added after Caddy's own compression;
    - `EDGE_TLS=files`;
    - the tunnel's token as a file (`TUNNEL_TOKEN_FILE`), never an environment value;
    - `cloudflared` sharing no network with `web` and publishing no port.
  - **Logs.** Both of Caddy's log filters delete `Cf-Access-Jwt-Assertion`.
  - **`files` mode.** No ACME, `default_sni {$APP_DOMAIN}`, and the key mode 0400, owned by the containers' uid 1000.
  - **Caddy itself** runs as uid 1000 with no capability, and a one-shot `caddy-perms` gives it its volumes. A release before M13.2 cannot read those volumes once converted (measured), so deploying one from its own checkout first gives them back to uid 0.
  - **Threat model.** [`docs/security.md`](../docs/security.md), *The edge*: (a)–(g).
- **Backups:** append-only offsite credentials on the VPS; pruning from the maintainer's machine (`--keep-within 90d`, `scripts/offsite_prune.py`, which refuses forged snapshots); dumps exclude sessions/cache; restore drills offline.
- **Deploys and restores:** nothing runs without its signature checked and no flag skips one; what a release states about its migrations is read only from the signed manifest, never the notes, and the image packages are public, so the VPS holds no token (ADR-0017); both gates on until the commit point; after any restore `post-restore-reconcile` runs before they open, and what could hand over an account (an email change, a removed second factor) waits for a human (ADR-0016). A new audit action that takes away access or a credential needs a handler in `apps.recovery.replay.HANDLERS`, and a new family of them in `ops/failed-state.sql`.

## Commands

```bash
python scripts/offsite_prune.py [--dry-run] [--baseline]   # M8.4: the maintainer's machine only, with the delete-capable restic credentials; refuses forged snapshots, and one new since its last run but dated before it (--baseline: once, the first time or after losing its record)
touch flags/worker.paused                           # the worker claims and sends nothing until removed
./deploy.sh X.Y.Z | ./recover.sh status              # production only, human only (M8.4); agents never run them outside a rehearsal
```

## Layout

- `docker_compose/gmat_m8/` (the stack: compose files and overlays incl. `docker-compose.local-mail.yml` (Mailpit), Caddyfile, env templates, `init.sh` (also adds new template keys to existing env files), `deploy.sh`, `recover.sh`, `restore-drill.sh` (M8.4; shared code in `ops/common.sh`, whose `backups_do` (M12.2) `init.sh` sources too, JSON and notes read by `ops/checks.py` on the host's python3, plain SQL against a failed database in `ops/*.sql`; a directory named `lib/` would be git-ignored)), `docker_compose/offsite/` (M8.4: the append-only REST server pair and the S3 policy)
