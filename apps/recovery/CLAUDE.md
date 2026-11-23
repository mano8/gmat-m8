# `apps/recovery`

What this app holds and how its parts fit. Loaded when you work in this directory; the
rules that hold everywhere are in the root `CLAUDE.md`. Keep this file current when the
app changes (introduced M8.4).

## Layout

- no models
- `services` (`post_restore_reconcile`: every session and pending token ended, recovery codes deleted and owed, the outbox quarantined, then the failed state replayed or, without one, privileged accounts suspended, in one transaction, by a platform operator; `confirm_suspended_account`, `suspended_accounts`)
- `failed_state` (what `ops/failed-state.sql` wrote)
- `replay` (the access-reducing and credential events the restored audit log lacks, in order; `HANDLERS`; what gave access is only reported; an email change or a removed second factor only suspends the account, and a human decides, `held_email_change`/`held_factor_removal`, ADR-0016)
- `manage post_restore_reconcile` (`entrypoint.sh post-restore-reconcile`)
- `manage confirm_account`
- `manage outbox_quarantine`

## Commands

```bash
uv run python manage.py outbox_quarantine --operator <email> list|release-notifications|discard-notifications|confirm-dispatch|release-dispatch|discard-dispatch [id] [--note …]   # M8.4: what a restore held back (notifications in bulk, dispatches one by one)
uv run python manage.py confirm_account --operator <email> --list | --email <account> [--credentials-changed] [--email-change accept|reject] [--factor-removal accept|reject]   # M8.4: accounts a restore suspended (without a failed database every privileged one; any restore, those with an email change or a removed second factor after the snapshot, which a human accepts or rejects, ADR-0016)
```
