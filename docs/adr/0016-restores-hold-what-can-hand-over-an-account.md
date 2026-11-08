# ADR-0016: A restore holds what can hand over an account

- **Status:** Accepted (the maintainer's decisions of 2026-11-06 on the phase's R3 review)
- **Date:** 2026-11-06
- **Governs:** `apps/recovery/`

## Context

After a restore, `post_restore_reconcile` replays the access-reducing and credential events that the failed database's audit log has and the restored one lacks (plan §9, step 5). Replaying them matters: without it, a revoked membership or a changed credential comes back with the backup.

That log is read from a database that may have been compromised. Anyone holding the runtime database credentials can add rows to it, and the replay applied them as facts. For most events that does little harm, because they only take access away. Two can hand an account over:

- **An email change** moved the account to the address in the row. A later password reset then goes to whoever owns that address.
- **A removed second factor** left only the password between the account and anyone who has it, and they could then enroll a factor of their own. Whoever holds those credentials can read the password hashes, and a planted row needs only one weak password to pay off. Three kinds of row do this: `user.mfa_reset`, `user.authenticator_removed`, and an earlier restore's "this account starts over" (`recovery.gap_credentials_reset`, `recovery.account_confirmed` with `credentials_changed`).

Doing nothing is no answer either. If the email change was genuine, the old mailbox may be why it was made. If a factor was removed because a phone was stolen, the restore brings that factor back.

## Decision

**Neither is applied. The account comes up suspended until a human decides.** While it waits, nobody signs in or gets a reset link, under any address.

- `manage confirm_account --list` shows what each account waits on. `--email-change accept|reject` and `--factor-removal accept|reject` decide it, and confirming refuses to go on while a question has no answer. `--credentials-changed` answers the factor question, since it removes every factor.
- Who decides follows the rules that already apply to these changes: `org.manage`, and a platform operator for a staff account.
- The password part of "starts over" stays automatic. An unusable password hands nothing to anyone.
- **The operator running the reconciliation stays signed in** and keeps their own address and factors, or nobody may be left to confirm anyone. The report names what was held for them (`own_email_change`, `own_factor_removal`) and nothing is applied.
- A row's address, account id and factor kinds are validated, because the row is untrusted. A malformed one is reported and changes nothing.
- A decision does not survive a second restore to an older backup: the event is replayed and held again, and the decision is made again. This follows the existing rule that a restore gives back no confirmation.

Events that only take access away (revocations, shorter validity, offboarding, deactivations, projects stopped, passwords made unusable) stay automatic. A planted one can lock people out, which a human then corrects, but it cannot hand anything to an attacker.

## Consequences

- Every genuine email change and factor removal in the gap needs a human step after a restore. That gap is usually hours, and restores are rare.
- A planted row can still suspend an account. That is visible in the report and undone by rejecting it.
- This amends plan §9, step 5, which applied both events.
