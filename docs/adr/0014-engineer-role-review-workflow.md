# ADR-0014: Engineer role, reviewer assignment, reviewer edits and self-decisions

- **Status:** Accepted (M7b.1; accepted by the maintainer with the M7b pull request, #28). §3 is amended by ADR-0019
- **Date:** 2026-11-02
- **Amends:** ADR-0010 (Q4), plan §3.4 (capability matrix, self-approval note) and §4.4 (editing rules of a material request)
- **Governs:** `apps/access/roles.py`, `apps/material_requests/reviewers.py`, `apps/material_requests/services/assignment.py`, `apps/material_requests/services/editing.py`, `apps/material_requests/services/review.py`

## Context

Issues #23 and #24 ask for three changes to the review of material requests:

- an **engineer** who checks and validates requests, ranked below the group administrator and above every other role;
- requests sent to a **named reviewer**, and reviewers who **edit** a request's lines, approved requests included, because supplier stock and availability change after approval;
- group administrators and material managers (and engineers) who **decide their own requests**. Today `allow_self_approval` on the procurement branch is the only way, and ADR-0010 Q4 keeps that rule unchanged.

The maintainer decided Q30-Q34 on 2026-11-01 (plan §10). Q35 and Q36 are working assumptions. Phase M7b builds the whole set: M7b.1 the role and the self-decisions, M7b.2 the assignment, M7b.3 the edits, M7b.4 the screens.

## Decision

### 1. The `ENGINEER` role (Q30, M7b.1)

- A **branch-level** membership (`branch` required, `company` null), scoped like `MATERIAL_MANAGER` (S1: the branch and the active branches it manages).
- **Rank** (maintainer, 2026-11-02): below `GROUP_ADMIN`, above every other role. The engineer holds **every capability of a material manager**, with the same reach, in its branch scope:
  - own requests (`request.create`, `request.withdraw`);
  - requests in scope (`request.view`, `request.review`, `request.edit`, `request.source`, `request.cancel_any`);
  - purchasing and prices (`po.view`, `po.manage`, `price.view`);
  - suppliers, catalog and projects (`supplier.view`, `supplier.manage`, `catalog.*`, `project.*`);
  - receipts and handovers (`receipt.*`, `handover.*`);
  - `audit.view` and `export.csv`.
- Only `org.manage` stays the group administrators'. A test keeps every other non-admin role's capabilities a subset of the engineer's, so a capability later given to a material manager (or anyone below) and not to engineers fails the build.
- The plan's first reading of Q30 (requests only, "to confirm in M7b.1") is replaced by this rank.
- What follows from these capabilities:
  - engineers read and moderate the attachments of the documents they reach (plan §5, the parent's capabilities);
  - they receive the `request.submitted` notices of their scope (M7.1, `request.review` holders);
  - they get every dashboard panel a material manager gets (M7.2).
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
- Details settled in M7b.2 (working assumptions, confirmed by the maintainer on 2026-11-02):
  - every assignment after the submit needs a note, the first one of an unassigned request included;
  - there is no "unassign": a reviewer reassigns, and a lapsed assignee falls back to unassigned on their own;
  - a resubmit after a return to `DRAFT` keeps the earlier assignee unless the requester picks another;
  - a requester who qualifies (an engineer, say) may pick themselves; nobody is mailed for assigning themselves;
  - at submit the assignee gets an "assigned to you" message instead of the general "to review" one.

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

- Details settled in M7b.3 (working assumptions, confirmed by the maintainer on 2026-11-02):
  - lines have no notes field (M4.1 kept it out), so an edit changes material, description, unit or quantity; its required reason is the reviewer's note;
  - `SUBMITTED`: an edit clears the line's decision whoever made it, the editor's own included; deciding in the same step takes `request.review`, and its note is the reason;
  - `APPROVED`: only a live line is edited; a new quantity becomes the requested and the approved quantity at once; without one, both stay (a partial approval stays partial) and must suit a new unit;
  - "nothing allocated" means no live allocation (a positive ceiling) on any order that is not cancelled, a closed order included: on a closed order a receipt void would revive pending of the old material;
  - an in-place material or unit change clears the line's sourcing (it was decided for the old material); a new description of a catalog line is not a material change;
  - `REPLACE`: the close reason is the edit's reason; the new line gets the edit's quantity or, in the same unit, A - max(D, R) (allocations are trimmed to what they received, so pending and reservations go); a line fully delivered is not replaced (add a line instead); the new line starts with sourcing undecided;
  - a new line (`ADD`, `REPLACE`) needs an active project (S9, C10); a quantity change does not;
  - a request submitted before M7b.3 has no copy: its first edit records one from the lines as they are then, marked `backfilled`;
  - `self_approved` means the editor decided the edited line themselves: always in `APPROVED`, in `SUBMITTED` when they approved or rejected it in the same step (a removal included);
  - a decision or an edit may carry the page's `seen_version` (submits plus edits); an older one is refused, so nobody approves a line they have not seen after another reviewer's edit;
  - a replaced line is closed short with source `LINE`, so `reopen_line` can reopen it next to its replacement (both needs live again); confirmed by the maintainer (2026-11-02): nothing blocks it.

### 6. The as-submitted copy and revisions (Q34, M7b.3, M7b.4)

- At every submit, each line's material, unit, description, quantity and notes are copied to an immutable, append-only row. A resubmit after a return to `DRAFT` starts a new baseline; earlier ones stay in the history.
- Every edit is a `MaterialRequestLineRevision` (append-only): editor, time, reason, kind (`CHANGE`, `ADD`, `REMOVE`, `REPLACE`), before/after values and `self_approved`.
- The requester's status page and the review page show, per line, the line as submitted, the current line and the edits. Technicians and partners see only fields they already see (no prices, suppliers or other projects). The requester is emailed for each batch of edits.
- Settled in M7b.3: the copy is `MaterialRequestBaseline` (the request's notes and submit) with `MaterialRequestBaselineLine` rows (material with its reference and name of that day, description, unit, quantity); both and `MaterialRequestLineRevision` are append-only by trigger and name a line by its id, without a foreign key, so deleting a draft line after a return for changes keeps the history of earlier submits. `selectors.line_history` gives each line with its copy from the latest submit and the edits since.
- A batch of edits is what happens while the requester's notice waits: the first edit queues one message, held back ten minutes, that only says the request changed and links to it; later edits find it waiting and queue nothing. Once the worker has claimed it, the next edit starts a new batch.

### 7. The screens (M7b.4)

- **Submit.** The review step has an optional "Send to a reviewer" choice (the reviewers `eligible_reviewers` lists for the branch the submit will take; no choice at all when none qualifies). A resubmit preselects the earlier assignee and then offers no "no one in particular" (no unassigning, §4).
- **Review page.** It names the assigned reviewer (or says that any reviewer of the branch can decide) and offers "Send to another reviewer" with a required note, to who may review the request. Per line: "Change the line" (a dialog: the material is searched with a GET form and chosen with a link, so it works without JavaScript; the description and unit of a described line; the quantity; a required reason), "Save and approve" next to "Save" in a submitted request for who may decide it (in an approved request the edit is approved with it), "Remove the line" with a reason, and "Add a line" for the request. There is no separate "replace" tool: the service decides whether a material or unit change is made in place or becomes a replacement (Q36), and the dialog says so in an approved request.
- **Stale pages and double taps.** The decision, approve-all and edit forms send the page's `seen_version`, which the screens require (a form without it is refused, never left unchecked); the edit, add and remove forms carry an idempotency key.
- **Quantities in the change dialog.** It shows the approved quantity on an approved request (an edit there sets requested and approved) and the requested one otherwise; the quantity is sent to the service only when it differs from what the dialog showed, so a material change on a line with deliveries keeps the default replacement quantity (what is still lacking). A material or unit in another unit asks for the quantity again (never carried over).
- **Reassigning** offers every eligible reviewer but the current assignee (assigning them again would change nothing and drop the note).
- **History.** The review page and the requester's status page show, for each edited line, the line as submitted, the edits (who, when, why, before and after) and an "Edited" marker; removed and replaced lines stay on the page. Only fields the line already shows.
- **Inbox and dashboard.** The inbox filters on "Assigned to me" and marks those rows; its status filter also lists rejected requests; the home page gets an "Assigned to me" panel offered to group administrators, material managers and engineers. **Decided by the maintainer (2026-11-02):** the panel counts the submitted, approved and rejected requests sent to the user; its chip is not an alert, since a rejected request asks for no action.
- Confirmed by the maintainer (2026-11-02): a rejected line of a submitted request offers a change but not a removal; an approved line that is fully delivered offers a change but not a removal; there is no separate "replace" tool.

## What this changes

- **ADR-0010 Q4:** "The self-approval rule (`allow_self_approval`) is unchanged" now applies to reviewers outside `SELF_DECIDING_ROLES` only; §3 above governs the others.
- **Plan §3.4:** the matrix gains the `ENGINEER` column and the `request.edit` row. The self-approval note is replaced by §3 above.
- **Plan §4.4:** "material, unit and requested quantity can change only in `DRAFT`" and "increasing an approved quantity is not possible" apply to **requesters** only. `request.edit` holders follow §5 above.

## Consequences

- Granting the first engineer is a privileged grant: that user must enroll MFA before using the role.
- Today an engineer and a material manager hold the same capabilities, because material managers also hold `request.edit` (Q32). The difference is the rank: an engineer must keep every capability of the roles below it, and future engineer-only rules (for example reviewing a material manager's request) build on it.
- Reversing `organizations.0003` fails once an engineer membership exists, because memberships are revoked, never deleted. The fix is a forward migration.
- Reviewer edits of approved lines change ledger inputs. M7b.3 is therefore built like the other ledger services (component lock, reconcile, floors) and tested with threaded races and hypothesis sequences. M8.1 depends on it.
- The acceptance matrix has an engineer actor (with and without session MFA), and the A/B/C fixture and `seed_demo` have an engineer of A-MAD.
