# Email

How mail leaves gmat-m8, what its states mean, and what a human does when a message is stuck, uncertain or held back after a restore.

## How it works, in one paragraph

A request never talks to SMTP. Business code writes an `EmailOutbox` row inside its transaction, and the **worker** container is the only sender: it claims a row, re-checks that the recipient is still allowed, records an attempt **before** any network I/O, sends through the relay in `app.env`, and records the outcome. Security mail (invitations, email changes, password resets) is sealed with `OUTBOX_ENCRYPTION_KEYS` and wiped as soon as it is sent, or after an hour. Supplier purchase orders are mailed only when `PO_DISPATCH=on`.

```bash
cd ~/gmat-m8/docker_compose/gmat_m8
alias dc='docker compose -f docker-compose.yml -f docker-compose.production.yml'
```

## States

| Status | Meaning | Becomes |
| --- | --- | --- |
| `QUEUED` | waiting for the worker (or for its `not_before` time) | `SENDING`, or a terminal state if it is no longer eligible |
| `SENDING` | an attempt is in progress | `SENT`, `FAILED`, `AMBIGUOUS` |
| `SENT` | the relay accepted it | final |
| `FAILED` | refused before the message data was sent (connection refused, definite rejection), or a sealed body that no key can open | retried by the worker up to 5 attempts, waiting 1 minute, then 2, 4, … up to 30 minutes; then it stays `FAILED` (security mail also stops when its hour is over) |
| `AMBIGUOUS` | timeout or disconnect **after** the data started, or the worker died mid-send. The relay may or may not have delivered | a business notification is retried once (a duplicate notification is acceptable); **security mail and supplier dispatches are never resent automatically** (see below) |
| `QUARANTINED` | held back by a restore | released or discarded by a human |
| `CANCELLED`, `SUPERSEDED`, `SUPPRESSED`, `EXPIRED` | final: cancelled with its order, replaced by a newer revision, nobody left to send to, or expired unsent (security mail after an hour) | final |

## Watching the outbox

The people-facing signal: an invitation or email change still *queued* after 10 minutes shows **"delayed"** on its screen. For the operator:

```bash
dc exec worker /app/docker/app/entrypoint.sh worker-healthcheck && echo "worker heartbeat fresh"
dc logs --since 30m worker
ls flags/                                   # worker.paused present?
grep -E '^OUTBOUND_EMAIL=' app.env           # off?

# A read-only count by kind and status, using the backup role:
dc exec db sh -c 'PGPASSWORD="$GMAT_DB_BACKUP_PASSWORD" psql -h 127.0.0.1 -U "$GMAT_DB_BACKUP_USER" -d "$POSTGRES_DB" -c \
 "SELECT kind, status, count(*), min(created_at) AS oldest FROM notifications_emailoutbox GROUP BY 1,2 ORDER BY 1,2"'
```

Alert yourself when: `QUEUED` is older than 10 minutes, any `AMBIGUOUS` or `QUARANTINED` row exists, or the heartbeat is stale. Message content is never in the log; an attempt is audited on the order by `system:worker` without address or body.

## When mail is not leaving

Work down the list; stop at the first thing you find.

1. **Paused?** `ls flags/`. `worker.paused` exists during a deploy or recovery, or when someone made it. If no deploy is running, `rm flags/worker.paused` resumes it (the worker reads it every loop).
2. **Off?** `OUTBOUND_EMAIL=off` in `app.env`: nothing is ever sent. Set `on`, then `dc up -d --no-deps --force-recreate worker`.
3. **Worker down?** `dc ps worker`, `dc logs worker`. Restart: `dc restart worker`.
4. **Relay refusing?** `dc logs worker` shows the stage (connect, STARTTLS, auth, rejected). Check `EMAIL_HOST`, port 587, `EMAIL_HOST_USER`, `SMTP_PASSWORD` in `.env`, the From address allowed by the relay, and the relay's own status page. The certificate must verify (exception X13): do not turn verification off.
5. **Specific recipients?** A recipient who is inactive, offboarded or lost the capability is dropped at the claim, on purpose: the message becomes `SUPPRESSED` if nobody is left.
6. **`FAILED` with "the body cannot be decrypted with any key"**: the outbox key changed. See [user-admin.md](user-admin.md#losing-a-key).

When the relay comes back, queued messages go on their own. Messages whose time has passed expire (security mail after an hour): re-issue those from the People screens.

## Supplier dispatches

Only when `PO_DISPATCH=on`. A dispatch (`PO_ORDER`) carries the order as it was issued or amended; recipients and content are frozen from the order's snapshots. The **order page** has a dispatch panel for a `po.manage` holder.

### `AMBIGUOUS`

The system does not know whether the supplier got it. It will **never** retry by itself, because a duplicate order can cost money. **Ask the supplier** (phone or a reply to their previous mail):

- *They have it*: on the order page, **Confirm** the dispatch, with a note ("confirmed by phone with X"). It becomes `SENT`.
- *They do not*: **Send again** with a note. It queues a new attempt (and it is sent only if this is still the order's current revision and the order is not cancelled; otherwise the page says the order changed and nothing is sent).
- *The order changed or was cancelled meanwhile*: nothing to do; the dispatch ends `SUPERSEDED` or `CANCELLED`, and a cancelled order owes a notice (below).

Every decision is audited on the order with your note.

### `FAILED`

The worker already tried up to 5 times with growing pauses, so this is a lasting problem: a relay that refuses, a rejected supplier address. Fix the cause (see [When mail is not leaving](#when-mail-is-not-leaving)) and **Send again** on the order page. The address is frozen from the order's snapshot, so sending again reuses it; if the address itself is wrong, send the printable order page (`orders/<id>/print/`) to the supplier by hand, and say so in the order's notes.

### Cancellation notices

Cancelling an issued order cancels every **unsent** dispatch in the same transaction and, if the supplier may already have the order (it was sent, ambiguous, or queued again by a human), creates one `PO_CANCELLATION` notice. That notice is owed and goes out even when `PO_DISPATCH=off`. Watch for it:

- If it ends `AMBIGUOUS` or `FAILED`, treat it exactly like an order dispatch: ask the supplier whether they know the order is cancelled, then confirm or send again. **Do not leave a supplier thinking an order is live**: call them.
- One notice per order (a unique rule); a second cancellation cannot create another.

## After a restore: quarantined messages

A restore marks everything unsettled `QUARANTINED` (except what the preserved failed database proves was finished, and security mail, which is wiped). Nothing is sent until a human decides. Run as a platform operator (it needs `org.manage` and `po.manage`):

```bash
dc run --rm --no-deps web manage outbox_quarantine --operator you@example.org list
```

It shows counts per kind and each supplier dispatch with its id, order and attempts.

| What | Command | Notes |
| --- | --- | --- |
| Business notifications, in bulk | `release-notifications` or `discard-notifications` | Release if people still need them ("request approved"); discard if the information is stale. When unsure after a long gap, discard: people see the same state on screen |
| One supplier dispatch | `confirm-dispatch ID` | the supplier confirmed they have it (→ `SENT`) |
| | `release-dispatch ID` | send it now; the supplier does not have it |
| | `discard-dispatch ID` | never send it (→ `CANCELLED`) |

Add `--note "…"` to each dispatch decision: it is kept in the order's audit trail. **Supplier dispatches one by one, after checking with the supplier.** A restore may have sent the mail before the snapshot or the loss, and a duplicate order is worse than a late one. Cancellation notices are among them: decide them with the same care.

A dispatch can also be confirmed or sent again from its order page; discarding is command-line only.

## Local and test stacks

Local and CI stacks deliver to Mailpit and never leave the machine (`docker-compose.local-mail.yml`; see the compose README). Production and the restore drill always pass `-f`, so Mailpit is never loaded there. Delete the `COMPOSE_FILE` lines from the VPS's `.env`.
