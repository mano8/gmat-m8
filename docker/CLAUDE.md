# `docker/`

The image sources. Loaded when you work in this directory; the rules that hold everywhere are in the root `CLAUDE.md`.

- `app/`: the application image (`Dockerfile`: the `runtime`, `dev` and `final` targets; `entrypoint.sh`, its commands and health checks; `gunicorn.conf.py`).
- `backup/`: the backup image (`backup.sh`, `restore.sh`, `common.sh`, `crontab`, `manifest.sql`).
- `postgres/`: the database's role bootstrap (`initdb`).

Before changing an image or an entrypoint, read the container rules in `docker_compose/CLAUDE.md` (*Rules in detail*: uids, read-only root filesystem, no capability, networks, sockets): the stack runs these images under them. Base images are pinned by digest (root `CLAUDE.md`, Supply chain) and `trivy-image` gates both images in CI. The scripts are bash for Linux, LF endings.
