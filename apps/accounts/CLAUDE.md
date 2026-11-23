# `apps/accounts`

What this app holds and how its parts fit. Loaded when you work in this directory; the
rules that hold everywhere are in the root `CLAUDE.md`. Keep this file current when the
app changes.

## Layout

- user
- `Invitation`
- `EmailChangeRequest`, allauth/MFA adapters, sessions, admin site
- `tokens` (token hashing, the invitation/email-change/bootstrap `TokenPrincipal`s)
- `services/` (a package: `common` the rules, reasons and locks; `grants`; `invitations` with acceptance and `bootstrap_admin`; `offboarding`; `credentials` the MFA reset (M10.4: `identity_checked` required, idempotency key, a staff account refused to a session principal, `gmat.security` `mfa_reset_by_admin`), `rename_user` (M10.4: a person's full name, one's own allowed, audited `user.renamed`), the encryption key and one's own sessions; `email_changes`)
- `selectors`
- `revocation` (pending tokens of an account or its initiator)
- `eligibility` (security-email rules)
- `housekeeping` (applies due email changes), token pages under `/invite/` and `/account/email-change/`
- `manage bootstrap_admin`
- `manage reset_mfa` (M8.3: `services.reset_user_mfa`)
- `manage reencrypt_mfa` (M8.5)
- `security_log` (M8.3: the `gmat.security` events, connected in `apps.py`; `warn_hsts_off`, called from `config/wsgi.py`)
- `sessions` (epoch, lifetime, idle, `touch_session`, `session_passive`), one's own sessions (M8.3: `selectors.own_sessions`, `services.end_own_session`/`end_other_sessions`, `session_urls` under `/account/sessions/`; allauth's `UserSession` rows, kept off the admin and out of dumps), housekeeping task `sessions` (expired sessions, rows without a session)
- `views.remove_webauthn` (replaces allauth's passkey removal, which never asked whether a privileged account keeps a factor)
- `createsuperuser` disabled

## Commands

```bash
uv run python manage.py reset_mfa --operator <admin email> --email <account> --reason "…" --identity-checked   # M8.3: remove a lost second factor, end the sessions, tell the owner (audited; org.manage); since M10.4 the person's page does it, the command stays for a staff account (a superuser operator) and break-glass
uv run python manage.py reencrypt_mfa --operator <admin email>   # M8.5: move every stored second-factor secret to the newest MFA key, so an old one can leave the list (idempotent; a second run says 0 moved)
```

## Rules in detail

The full text of the root `CLAUDE.md`'s rules this directory implements; the root keeps a one-sentence summary of each.

- **People (ADR-0029):** a person is shown by name (`apps.accounts.people.name_of`, `{% load people %}` `user|person`; "—" for a legacy account without one), never by address: the name is required to invite (`create_invitation`) and for `bootstrap_admin --full-name`. Another person's address only to group administrators on the `/org/` people and invitation screens and, as the requester's, on the review page to a reviewer through a managing role (`selectors.shows_requester_address`, `user|person_contact`); audit timelines name the actor (`recorded_name`). Kept: the requests CSV export, the audit viewer's recorded labels and the admin. `tests/access/test_people_addresses.py` reads every matrix URL as each non-managing actor.
- **Auth:** django-allauth ≥ 65.20.1 (CI floor), admin login through allauth; a password or second factor that changes tells its owner (`ACCOUNT_EMAIL_NOTIFICATIONS`, through the outbox); allauth email views off (admin-initiated email change: new address confirms, old can cancel within 24 h, authority re-checked); reset never logs in, no login-by-code; `__Host-` cookies; reset tokens re-validated under the user-row lock (single use, 1 h); "MFA required" = this session passed MFA; MFA secrets encrypted with `MFA_ENCRYPTION_KEYS`; rate limits on `SecurityDatabaseCache` (429 when full); `ALLAUTH_TRUSTED_PROXY_COUNT=1`.
- **Accounts:** offboarding revokes the user's pending invitations and email changes and those they initiated; token redemption locks target and initiator users (id order) then the token, re-checks authority, never changes an active account's credentials; tokens 32 random bytes, hash stored, constant-time compare; first admin only via `manage bootstrap_admin`; `createsuperuser` disabled.
