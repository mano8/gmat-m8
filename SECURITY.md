# Security policy

## Reporting a vulnerability

Please do **not** open a public issue, pull request or discussion for a security problem.

Report it privately through GitHub's private vulnerability reporting:
[open a draft security advisory](https://github.com/mano8/gmat-m8/security/advisories/new) (repository → **Security** → **Report a vulnerability**).

A useful report says:

- what is affected (URL, view, service, command, image or compose service) and the commit or image digest you tested;
- how to reproduce it, with the role of the account used (technician, material manager, group administrator, partner, site manager, …);
- the impact: which access control or secret the attack defeats. Typical cases are reading data of another company, branch or project; seeing prices or suppliers the role should not see; writing without the required capability; bypassing the session or multi-factor authentication (MFA) checks; leaking a token.

Test only on a local stack or a deployment you operate. If you found the problem on someone else's deployment, stop there and report what you observed. Leave real personal data and production data out of the report; describe them instead (for example "the email address of another branch's user").

## What happens next

The project has one maintainer, so handling is best effort, with no fixed response time.

1. The maintainer acknowledges the report in the advisory.
2. The fix is prepared in the advisory's private fork.
3. The advisory is published once a fixed version is available.

Say in the report whether you want to be credited.

## Supported versions

No version has been released yet. Until then only the `main` branch is supported. Once releases exist, the latest release is supported; fixes are not backported unless the advisory says so.

## Scope

In scope: the code in this repository, the images built from it (`docker/app`, `docker/backup`) and the compose stack in `docker_compose/gmat_m8`.

A vulnerability in a dependency (Django, django-allauth, htmx, PostgreSQL, Caddy, …) belongs upstream. Report it here as well when gmat-m8 uses the affected part in a way that makes it exploitable.

## Further reading

- [docs/security.md](docs/security.md): the security baseline the application enforces (authentication, MFA, sessions, authorization, headers, logging, email, containers).
- [docs/release.md](docs/release.md): how images are built, signed and verified.
