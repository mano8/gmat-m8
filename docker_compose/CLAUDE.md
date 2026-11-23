# `docker_compose/` and `docker/`

The stack, its images and the host scripts. Loaded when you work in this directory; the rules that hold everywhere are in the root `CLAUDE.md`. The commands are in the root (Commands) and in `gmat_m8/README.md`.

## Rules in detail

The full text of the root `CLAUDE.md`'s rules this directory implements; the root keeps a one-sentence summary of each.

- **Containers:** non-root uid 1000 (the `decoder` uid 1001, `clamd`/`freshclam` their image's uid 100, `db` its image's uid 70, no capability, so `gosu` never runs), read-only root fs; only Caddy publishes ports; only Caddy and `web` share `edge`; each DB client (`web`, `worker`, `backup`, `migrate`) has its own internal network; DB never published; what parses uploads (`clamd`, `decoder`) has no network and no secret, and `web` reaches it through a unix socket on a volume only the two mount, read-only on `web`'s side (ADR-0015).
- **Backups:** append-only offsite credentials on the VPS; pruning from the maintainer's machine (`--keep-within 90d`, `scripts/offsite_prune.py`, which refuses forged snapshots); dumps exclude sessions/cache; restore drills offline.
- **Deploys and restores:** nothing runs without its signature checked and no flag skips one; what a release states about its migrations is read only from the signed manifest, never the notes, and the image packages are public, so the VPS holds no token (ADR-0017); both gates on until the commit point; after any restore `post-restore-reconcile` runs before they open, and what could hand over an account (an email change, a removed second factor) waits for a human (ADR-0016). A new audit action that takes away access or a credential needs a handler in `apps.recovery.replay.HANDLERS`, and a new family of them in `ops/failed-state.sql`.

## Commands

```bash
python scripts/offsite_prune.py [--dry-run] [--baseline]   # M8.4: the maintainer's machine only, with the delete-capable restic credentials; refuses forged snapshots, and one new since its last run but dated before it (--baseline: once, the first time or after losing its record)
touch flags/worker.paused                           # the worker claims and sends nothing until removed
./deploy.sh X.Y.Z | ./recover.sh status              # production only, human only (M8.4); agents never run them outside a rehearsal
```

## Layout

- `docker_compose/gmat_m8/` (the stack: compose files and overlays incl. `docker-compose.local-mail.yml` (Mailpit), Caddyfile, env templates, `init.sh` (also adds new template keys to existing env files), `deploy.sh`, `recover.sh`, `restore-drill.sh` (M8.4; shared code in `ops/common.sh`, JSON and notes read by `ops/checks.py` on the host's python3, plain SQL against a failed database in `ops/*.sql`; a directory named `lib/` would be git-ignored)), `docker_compose/offsite/` (M8.4: the append-only REST server pair and the S3 policy)
