# Security policy

## Reporting a vulnerability

Please do **not** open a public issue, pull request or discussion for a security problem.

Report it privately through GitHub's private vulnerability reporting:
[open a draft security advisory](https://github.com/mano8/gmat-m8/security/advisories/new) (repository → **Security** → **Report a vulnerability**).

A useful report says:

- what is affected (URL, view, service, command, image or compose service) and the commit or image digest you tested;
- how to reproduce it, with the role of the account used (technician, material manager, group administrator, partner, site manager, …);
- what an attacker gains: data of another company, branch or project, prices or suppliers a role should not see, a write without the capability, a session or MFA bypass, a token leak, and so on.

Never test against a deployment you do not operate, and do not include real personal data or production data in the report.

## What happens next

The project has one maintainer, so handling is best effort, with no fixed response time. The maintainer acknowledges the report in the advisory, works on the fix there (a private fork if needed), and publishes the advisory when a fixed version is available. Tell us in the report if you want to be credited.

## Supported versions

No version has been released yet. Until then only the `main` branch is supported. Once releases exist, the latest release is supported; fixes are not backported unless the advisory says so.

## Scope

In scope: the code in this repository, the images built from it (`docker/app`, `docker/backup`) and the compose stack in `docker_compose/gmat_m8`.

A vulnerability in a dependency (Django, django-allauth, htmx, PostgreSQL, Caddy, …) belongs upstream. Report it here as well when gmat-m8 uses the affected part in a way that makes it exploitable.

## Further reading

- [docs/security.md](docs/security.md): the security baseline the application enforces (authentication, MFA, sessions, authorization, headers, logging, email, containers).
- [docs/release.md](docs/release.md): how images are built, signed and verified.
