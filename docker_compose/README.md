# Docker Compose

| Directory | What it is |
| --- | --- |
| [gmat_m8/](gmat_m8/README.md) | The full gmat-m8 stack: Caddy, web, PostgreSQL, backup (and the worker from M2.5), ready to use. |

The stack is the deployment artifact: the same files run on a laptop (`localhost`, Caddy's internal CA, images built locally) and on the VPS (the production overlay, images pulled by signed digest). Image sources are in [`../docker/`](../docker/); the decisions behind the layout are in plan §7 and the security baseline in [`../docs/security.md`](../docs/security.md).
