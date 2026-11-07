# Restore drill log

One line per run of the quarterly offsite restore drill ([backup-restore.md](backup-restore.md#quarterly-offsite-drill)). The drill is run by the maintainer on an isolated machine; `restore-drill.sh` prints the line to add. Add it in a PR the same day, with a short note under the table for anything that was not clean.

A drill is **due every quarter**. A `FAILED` verdict is an incident ([incident.md](incident.md)); the next line records the drill that passed after the cause was fixed.

| Date (UTC) | Dump | App image digest | Verdict |
| --- | --- | --- | --- |

## Notes

No drill has run against real data yet: the first one is part of the go-live (plan H8.7), right after the first deploy and before real data is loaded. The scripts themselves were rehearsed against real containers on 2026-11-05 (37 tests, 38 drill checks passed): see [../deploy-rehearsal.md](../deploy-rehearsal.md). That rehearsal does not replace a drill, because it cannot prove the real offsite storage, the real keys or the real images.

What a line means: `PASSED` is `restore-drill.sh` exiting 0, with every check reported `PASS`: the runtime role writes, the audit table refuses `UPDATE` and `DELETE`, row counts equal the manifest, attachments match their checksums, the pre-restore session, invitation, reset link, signed key and recovery codes are rejected, revocations are re-applied or privileged accounts are suspended, queued messages are `QUARANTINED` and nothing was sent, and `web` and the worker cannot reach the outside.
