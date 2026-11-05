# Offsite backup storage

The VPS writes its backups to a restic repository with credentials that **cannot delete or overwrite anything** (plan §9, F61). A compromised VPS therefore cannot destroy the offsite history. `forget` and `prune` run only from the maintainer's machine, with separate credentials that never touch the VPS. Pick one of the two setups below when provisioning (plan H8.6).

## Option A: restic REST server

`docker-compose.rest-server.yml` runs two servers on one data directory, on a host that is not the VPS. `rest-append` runs with `--append-only` and is what the VPS reaches. `rest-admin` can delete, listens on 127.0.0.1 only, and is reached through an SSH tunnel from the maintainer's machine.

1. Create the users, one password file per server:

   ```bash
   mkdir -p auth data && chown 1000:1000 data
   docker run --rm -it -v "$PWD/auth:/auth" --entrypoint htpasswd restic/rest-server:0.14.0 -cB /auth/append.htpasswd gmat
   docker run --rm -it -v "$PWD/auth:/auth" --entrypoint htpasswd restic/rest-server:0.14.0 -cB /auth/admin.htpasswd gmat
   ```

2. Start them and put TLS in front of `rest-append`: `docker compose -f docker-compose.rest-server.yml up -d`.
3. On the VPS, in `.env`:

   ```bash
   RESTIC_REPOSITORY=rest:https://backup.example.org/gmat/gmat
   RESTIC_PASSWORD=<offline copy kept by the maintainer>
   RESTIC_REST_USERNAME=gmat
   RESTIC_REST_PASSWORD=<the append password>
   ```

4. On the maintainer's machine: `ssh -L 8001:127.0.0.1:8001 backup-host`, then prune through `rest:http://127.0.0.1:8001/gmat/gmat` with the admin password.

With `--private-repos` each user reaches only `/<username>/…`, which is why the path repeats the name.

## Option B: an S3 bucket

Turn on **versioning** and **Object Lock in compliance mode with a 90-day default retention**: the storage itself then enforces the protected window, whatever anyone holding a key does (F72).

- The VPS's key gets `s3-vps-policy.json` (replace `BUCKET`): it can read and add objects and can delete nothing. restic's lock files stay behind as delete markers or new versions and are harmless; `restic unlock` runs from the maintainer's machine.
- The maintainer's key, which can delete, stays on the maintainer's machine and is never written to the VPS.

```bash
RESTIC_REPOSITORY=s3:https://s3.example.org/BUCKET/gmat
RESTIC_PASSWORD=<offline copy kept by the maintainer>
AWS_ACCESS_KEY_ID=<the VPS's key>
AWS_SECRET_ACCESS_KEY=<the VPS's secret>
AWS_DEFAULT_REGION=<region>
```

## Pruning, from the maintainer's machine only

```bash
export RESTIC_REPOSITORY=rest:http://127.0.0.1:8001/gmat/gmat
export RESTIC_PASSWORD=...           # the offline copy
export RESTIC_REST_USERNAME=gmat
export RESTIC_REST_PASSWORD=...      # the admin password, never on the VPS
python scripts/offsite_prune.py --dry-run
python scripts/offsite_prune.py
```

`scripts/offsite_prune.py` needs `restic` on PATH and only the Python standard library. It refuses to run if any snapshot has an unexpected host or tag, a timestamp in the future or an abnormal count per day: such snapshots are investigated, never pruned around. Forged snapshots can fill the weekly and monthly rules, so that an older real snapshot loses its place and goes at the next prune; `--keep-within` only protects the last 90 days. The script then keeps every snapshot of the last 90 days plus 4 weekly and 12 monthly ones, checks restic's plan against that window, forgets exactly the snapshots it checked, and lists the repository again: restic exits 0 even when a server refuses the removal.

To see that the VPS's credentials cannot delete, look at the 403 and at the listing, not at the exit code, for the same reason.
