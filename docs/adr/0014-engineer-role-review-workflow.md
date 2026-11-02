# ADR-0014: Engineer role, reviewer assignment, reviewer edits and self-decisions

- **Status:** Proposed (M7b.1; accepted by the maintainer with the M7b pull request)
- **Date:** 2026-11-02
- **Amends:** ADR-0010 (Q4), plan §3.4 (capability matrix, self-approval note) and §4.4 (editing rules of a material request)

## Context

Issues #23 and #24 ask for three changes to the review of material requests:

- an **engineer** who checks and validates requests without buying, receiving or handing anything over;
- requests sent to a **named reviewer**, and reviewers who **edit** a request's lines, approved requests included, because supplier stock and availability change after approval;
- group administrators and material managers (and engineers) who **decide their own requests**. Today `allow_self_approval` on the procurement branch is the only way, and ADR-0010 Q4 keeps that rule unchanged.

The maintainer decided Q30-Q34 on 2026-11-01 (plan §10). Q35 and Q36 are working assumptions. Phase M7b builds the whole set: M7b.1 the role and the self-decisions, M7b.2 the assignment, M7b.3 the edits, M7b.4 the screens.

## Decision

### 1. The `ENGINEER` role (Q30, M7b.1)

- A **branch-level** membership (`branch` required, `company` null), scoped like `MATERIAL_MANAGER` (S1: the branch and the active branches it manages).
- Capabilities. Own requests: `request.create` and `request.withdraw`. In scope: `request.view`, `request.review`, `request.edit`, `project.view` and `catalog.view`.
- None of these: sourcing (`request.source`), cancelling other users' requests (`request.cancel_any`), purchasing (`po.*`), prices, suppliers, receipts, handovers, `project.manage`, `catalog.manage`, `audit.view`, `export.csv`.
- What follows from these capabilities:
  - engineers read the attachments of the requests they see and add to their own (plan §5, the parent's capabilities);
  - they receive the `request.submitted` notices of their scope (M7.1, `request.review` holders);
  - they get the inbox and "my requests" dashboard panels (M7.2).
- **Privileged**: in `PRIVILEGED_ROLES`. That means session MFA on every page and in `authorize_in_txn`, the 12-hour session (Q12), and `privilege_elevated_at` moved at every grant.
- Only `org.manage` grants memberships, and only `GROUP_ADMIN` holds it. A material manager therefore cannot make anyone an engineer, and an invitation that grants the role is checked against its initiator's `org.manage` (F71).
- Migration `organizations.0003_engineer_role`: the role CHECK accepts the new value and a shape CHECK binds engineer rows. It only expands the schema: the previous release keeps working.

### 2. `request.edit` (Q32)

A new capability held in scope by `GROUP_ADMIN`, `MATERIAL_MANAGER` and `ENGINEER`. It is the reviewer's line editing of §4 below. Its services arrive with M7b.3; until then nothing checks it.

### 3. Self-decisions (Q33, #23, M7b.1)

- `SELF_DECIDING_ROLES` (`GROUP_ADMIN`, `MATERIAL_MANAGER`, `ENGINEER`) approve or reject the lines of **their own** request, whatever the procurement branch's `allow_self_approval` says. M7b.3 extends the same rule to approving their own edits.
- **Role source:** the role of a membership whose `request.review` grant covers **that** request (`ctx.scope_for(...)`, S6). A role held through another membership gives nothing. Example: someone who is site manager of the hospital and material manager of A-SEV still cannot decide their own hospital request, because A-MAD buys for the hospital.
- **`is_superuser` never authorizes** (§3.6). A platform superuser gets the exemption only through a `GROUP_ADMIN` membership.
- **MFA:** the exempt roles are privileged, so a self-decision needs a session that passed MFA after `privilege_elevated_at`. This holds even when an ordinary grant (a site manager's) let `authorize_in_txn` through. The check is `apps.access.authorization.authorize_self_decision`, run after `authorize_in_txn` in the same transaction; the review page's `can_decide` mirrors it.
- **Other reviewers** (site managers) keep `allow_self_approval` on the procurement branch (working assumption of Q33). The flag stays; nothing is migrated.
- **Audit:** a decision by the requester carries `self_decided: true` on each `request_line.decided` event and on the `request.approved` / `request.rejected` event that finalises the request. A decision on someone else's request carries no marker.
- **Q4 still holds.** Every request is reviewed and a submit decides nothing: the requester decides explicitly on the review page.

### 4. Reviewer assignment (Q31, Q35, M7b.2)

- `MaterialRequest.assigned_reviewer` (+ `assigned_at`, `assigned_by`). The requester may pick a reviewer at submit; any reviewer in scope may reassign later, in `SUBMITTED` and `APPROVED`, with a note (audited).
- The assignee must hold `request.review` through a `GROUP_ADMIN`, `MATERIAL_MANAGER` or `ENGINEER` membership whose scope covers the request's procurement branch. This is checked when assigning (S6, S8).
- A revoked or expired assignee no longer counts: the request reads as unassigned, and nothing is rewritten.
- The assignment **never restricts who decides**: any reviewer in scope still can. One level only; sequential approvals stay backlog item B10.
- Q35 (working assumption): every requester may pick a reviewer (optional). An unassigned request stays in every in-scope reviewer's inbox.
- The assignee is emailed through the outbox (M7.1), with eligibility checked again at the claim.

### 5. Reviewer edits (Q32, Q36, M7b.3)

- `request.edit` holders change a line's material, unit, quantity or notes, add a line or remove one, in `SUBMITTED` and `APPROVED`. Each edit needs a reason and follows the full write order of plan §4.7: `authorize_in_txn` → idempotency → component lock through `commitments` → validate → write → reconcile → audit and event.
- `SUBMITTED`: an edit resets another reviewer's decision on that line to `PENDING`. The editor may approve or reject the edited line in the same call.
- `APPROVED`: the editor's edit is approved with it (Q33).
  - Quantity never goes below max(D, R) + P + Q.
  - An increase raises both the requested and the approved quantity and reconciles the component.
- Q36 (working assumption):
  - A material or unit change happens in place only while the line has nothing allocated, received or delivered.
  - Otherwise the edit is a `REPLACE`: the line is closed short at its floor (`closed_short_source=LINE`, reason "replaced by edit") and a new approved line takes the rest (C9 holds).
  - Removing a line rejects it (`SUBMITTED`) or closes it short (`APPROVED`); it stays visible and is never deleted.
  - `CLOSED`, `REJECTED` and `CANCELLED` refuse edits (reopen first).

### 6. The as-submitted copy and revisions (Q34, M7b.3, M7b.4)

- At every submit, each line's material, unit, description, quantity and notes are copied to an immutable, append-only row. A resubmit after a return to `DRAFT` starts a new baseline; earlier ones stay in the history.
- Every edit is a `MaterialRequestLineRevision` (append-only): editor, time, reason, kind (`CHANGE`, `ADD`, `REMOVE`, `REPLACE`), before/after values and `self_approved`.
- The requester's status page and the review page show, per line, the line as submitted, the current line and the edits. Technicians and partners see only fields they already see (no prices, suppliers or other projects). The requester is emailed for each batch of edits.

## What this changes

- **ADR-0010 Q4:** "The self-approval rule (`allow_self_approval`) is unchanged" now applies to reviewers outside `SELF_DECIDING_ROLES` only; §3 above governs the others.
- **Plan §3.4:** the matrix gains the `ENGINEER` column and the `request.edit` row. The self-approval note is replaced by §3 above.
- **Plan §4.4:** "material, unit and requested quantity can change only in `DRAFT`" and "increasing an approved quantity is not possible" apply to **requesters** only. `request.edit` holders follow §5 above.

## Consequences

- Granting the first engineer is a privileged grant: that user must enroll MFA before using the role.
- Reversing `organizations.0003` fails once an engineer membership exists, because memberships are revoked, never deleted. The fix is a forward migration.
- Reviewer edits of approved lines change ledger inputs. M7b.3 is therefore built like the other ledger services (component lock, reconcile, floors) and tested with threaded races and hypothesis sequences. M8.1 depends on it.
- The acceptance matrix has an engineer actor (with and without session MFA), and the A/B/C fixture and `seed_demo` have an engineer of A-MAD.
