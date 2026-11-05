# User administration

Everyday account work, and the rare cases: a lost phone, a leaver, a lost or rotated encryption key. The screens are for group administrators (capability `org.manage`); the commands are for the maintainer on the VPS.

Commands that start with `dc` use the production compose invocation:

```bash
cd ~/gmat-m8/docker_compose/gmat_m8
alias dc='docker compose -f docker-compose.yml -f docker-compose.production.yml'
# a management command:   dc run --rm --no-deps web manage <command> ...
```

Every `--operator EMAIL` below is an existing, active account that holds the capability the command needs (`org.manage` for most). The command runs **as that person** and the audit trail names them. Use your own account, never a shared one.

## The first administrator

Done once, after the first deploy. No account exists yet, so the command prints a one-time setup link in your terminal (valid 72 hours, shown only there):

```bash
dc run --rm web manage bootstrap_admin --email you@example.org --company-code A --company-name "Company A" --superuser
```

- `--superuser` also makes the account a **platform operator**. The recovery tooling (`recover.sh`, `post-restore-reconcile`) needs one, because it reconciles as a platform operator. Create at least one and keep it. The command refuses once an active administrator (or, with `--superuser`, an active platform operator) exists.
- Open the link in a browser, set a password (12 characters at least), and the first sign-in enrolls MFA. Do it straight away and store the recovery codes offline.
- If the link is lost before use, run the command again: it replaces the link.
- `createsuperuser` is disabled on purpose. The Django admin needs a group administrator or platform operator with an MFA-verified session and an address in `ADMIN_ALLOWED_CIDRS`.

Everyone else is invited from the app.

## Inviting a person

People → **Invite someone** (`/org/invitations/new/`). Choose the email, the role and where it applies. Rules the screen enforces:

- An invitation lives **72 hours**. The person opens the link, sets a password, and for a privileged role (`GROUP_ADMIN`, `MATERIAL_MANAGER`, `ENGINEER`) enrolls MFA at the first sign-in. The link works once.
- A group administrator cannot invite or change a staff or superuser account.
- An existing active person accepts an invitation only while signed in as themselves, and their credentials are never changed by it. A person who was offboarded can be re-hired only by an invitation created after the offboarding.
- Email is sent by the worker, not the page. The invitation shows a delivery badge: *queued*, *sent*, *delayed* (still queued or sending after 10 minutes), or failed. **"Sent" means the relay accepted it**, not that the person read it.

If it does not arrive:

1. *Queued* for long, or *delayed*: the worker is paused, outbound mail is off, or the relay is down. See [email.md](email.md#when-mail-is-not-leaving).
2. *Sent* but nothing in the inbox: ask the person to check their spam folder and the address, then **Resend** on the invitations list (a new link; the old one stops working), or **Revoke** and invite again with the right address.
3. A link that says it is invalid: expired (72 hours), already used or revoked. Invite again.

Never paste a link into a chat or ticket: it is a credential. If you must give one by phone, resend a fresh invitation afterwards.

## Changing a person's role or scope

People → the person: **grant** a membership, set its validity dates, or **revoke** it. Memberships are revoked, never deleted, and the history stays. Changes apply on the person's next request. A person's capabilities are never mixed across memberships: each applies only in its own scope. Giving a privileged role requires the person to pass MFA again before using it.

## Offboarding

When someone leaves: People → the person → **Offboard**. In one transaction it deactivates the account, ends all its sessions, revokes every membership and project assignment, revokes its pending invitations and email changes **and those it issued**, and wipes unsent security email addressed to it. The account and its history stay (users are deactivated, never deleted, for the audit trail).

- You cannot offboard yourself, so keep at least two administrators: if one of them leaves, the other offboards them.
- Do it the day the person leaves, before the exit meeting if the circumstances need it.
- A staff or superuser account can be offboarded only by a superuser.
- To bring someone back, invite them again after the offboarding (see above).

## Changing an email address

A group administrator starts it from the person's page (**Change email**). It is deliberately slow:

1. The **new** address gets a confirmation link (24 hours, single use).
2. The **old** address gets a cancel link, valid until the change applies. This is how a hijacked change is stopped.
3. Once the new address confirms, there is a 24-hour cooling-off. The worker's hourly housekeeping then applies it, only if the account is still active, the initiator still has authority, and the address is still free. Applying ends all of the person's sessions.

Revoke a pending change from the same page. A person who cannot receive on the old address anymore is not a reason to skip the cooling-off; if urgent, offboard and re-invite on the new address instead.

## Password resets

The person uses **Forgot password** on the sign-in page. A reset never signs them in and the link lives one hour. Inactive accounts get no reset mail. There is nothing for an administrator to do except: *do not set passwords by hand on a shell* (`manage changepassword`). It writes no audit event, so a later restore cannot replay it. Offboard and re-invite instead.

## Lost second factor

Check the person's identity **out of band** (call a number you already had, not one they give you now; or ask a colleague who knows them). Then:

- If they have another authenticator or a recovery code, they sign in with it and manage factors themselves. A privileged account cannot remove its last factor.
- Otherwise reset them:

  ```bash
  dc run --rm --no-deps web manage reset_mfa --operator you@example.org --email person@example.org --reason "lost phone, identity checked by phone with branch manager"
  ```

  It removes every second factor of the account, ends its sessions and tells the owner by email (without the reason or who did it). The reason is kept in the audit trail. At the next sign-in the account enrolls a new factor where its roles need one. Tell them to keep the new recovery codes offline.
- A staff or superuser account needs a superuser operator.

## Sessions, inactivity and notices

- **Inactivity.** A session ends after 30 minutes idle on a privileged account and 2 hours on the others, on top of the 12-hour and 14-day limits. Change them in `app.env` (`DJANGO_PRIVILEGED_SESSION_IDLE_SECONDS`, `DJANGO_SESSION_IDLE_SECONDS`, at least 300). A status poll on a page left open does not count as activity. A person who complains of being signed out often: that is this; look at the security log's `session_ended` lines, which give the reason (`idle`, `lifetime`, `epoch`).
- **Their own sessions.** Everyone has **Sessions** in the menu: where they are signed in, from which address and browser, with a button to end one or all the others. Ask a person who lost a phone or suspects something to start there and change their password. You cannot see or end someone else's from it; offboarding or an MFA reset ends all of a person's sessions.
- **Notices.** A password changed or reset, a second factor added or removed, and new recovery codes each send the owner an email with the address and browser it came from. They go through the outbox like all account mail. A person who did not do it should tell you straight away: treat it as an **Account** incident ([incident.md](incident.md)).

## Accounts a restore suspended

After a restore without a preserved failed database, every privileged account is suspended until confirmed:

```bash
dc run --rm --no-deps web manage confirm_account --operator you@example.org --list
dc run --rm --no-deps web manage confirm_account --operator you@example.org --email person@example.org [--credentials-changed]
```

Ask each person whether their password or authenticator changed since the snapshot. If it did, or you are not sure, use `--credentials-changed`: the account gets an unusable password and loses its authenticators, and the person resets through a fresh invitation. See [backup-restore.md](backup-restore.md#after-any-restore).

## Encryption keys: MFA and outbox

Two Fernet key lists in `app.env`, both newest first, and **different from each other** (the app refuses equal keys):

| Setting | Protects | Used by |
| --- | --- | --- |
| `MFA_ENCRYPTION_KEYS` | every stored TOTP secret and recovery-code seed | `web` (sign-in, MFA screens) |
| `OUTBOX_ENCRYPTION_KEYS` | the bodies of security email (reset links, invitations) for at most an hour | `web` (writing) and the worker (reading) |

Generate a key with `openssl rand -base64 32 | tr '+/' '-_'`. The first key in a list encrypts; **every** key in it decrypts.

### Applying a change to a list

Edit `app.env` (mode 0600), then recreate the two services that read it, outside working hours (about 15 seconds of interruption):

```bash
dc up -d --no-deps --force-recreate web worker
dc ps          # healthy
```

Keep a copy of the new `app.env` offline, **before** the restart.

### Rotating the outbox key (routine, or after a suspected leak)

Sealed bodies live one hour at most.

1. Put the new key in front: `OUTBOX_ENCRYPTION_KEYS=<new>,<old>`. Recreate `web` and `worker`.
2. Wait **at least 90 minutes**: every body sealed with the old key is gone by then.
3. Remove the old key: `OUTBOX_ENCRYPTION_KEYS=<new>`. Recreate again.

### Rotating the MFA key

1. Put the new key in front: `MFA_ENCRYPTION_KEYS=<new>,<old>`, recreate `web` and `worker`. New enrollments use the new key; existing ones still open with the old one. Save the new list offline first.
2. Move everything to the new key:

   ```bash
   dc run --rm --no-deps web manage reencrypt_mfa --operator you@example.org
   ```

   It prints how many secrets moved. It is safe to repeat, and a second run must say `0 moved`: a sign-in in progress during the first run may have written an old value back. If it lists accounts that no listed key opens, keep every old key, find the missing one or `reset_mfa` those accounts.
3. Remove the old key: `MFA_ENCRYPTION_KEYS=<new>`, recreate `web` and `worker`. Sign in with a second factor to check.
4. **Keep the old key offline for 90 days.** Backups taken before step 2 hold secrets under it, and a restore of one needs it listed again: put it back behind the new key, restore, and run `reencrypt_mfa` once more.

If the old key may have **leaked**, moving secrets to a new key is not enough: an attacker with the old key and a copy of the database can still read them, and they are the same secrets. Rotate as above, then `reset_mfa` every account that had a factor, starting with your own operator account from a second, verified session, so that everyone enrolls a new secret.

### Losing a key

- **`MFA_ENCRYPTION_KEYS` lost** (no copy anywhere): every stored secret is unreadable and each user fails MFA with an error. Generate a new key, set it, recreate the services, then `reset_mfa` for every account that had a factor, starting with your own operator account (the command line does not need MFA). Everyone enrolls again.
- **`OUTBOX_ENCRYPTION_KEYS` lost**: messages sealed with it end `FAILED` ("the body cannot be decrypted with any key"). Set a new key, recreate the services, and re-send the invitations and email changes people were waiting for from the People screens. Nothing else is lost; business notifications are not sealed.
- The key in `app.env` and its offline copy must always be the same. Check after each change: compare the lists, never print them in a shared terminal.

## Quick reference

| Task | Where |
| --- | --- |
| Invite, resend, revoke | People → Invitations |
| Roles and scope | People → the person |
| Leaver | People → the person → Offboard |
| New address | People → the person → Change email |
| Lost phone | `manage reset_mfa` |
| Retiring an MFA key | `manage reencrypt_mfa` |
| A person's own sessions | Sessions, in their own menu |
| After a restore | `manage confirm_account` |
| First administrator | `manage bootstrap_admin` |
| Bulk master data | `manage import_catalog | import_suppliers | import_projects <file> --operator EMAIL [--dry-run]`, templates in `docs/import-templates/` |
