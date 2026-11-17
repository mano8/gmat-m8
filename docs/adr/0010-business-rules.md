# ADR-0010: Business rules: H0.1 answers and working assumptions

- **Status:** Accepted
- **Date:** 2026-10-26
- **Governs:** `apps/access/roles.py`, `apps/material_requests/transitions.py`, `apps/purchasing/transitions.py`, `apps/purchasing/dispatch.py`, `apps/purchasing/services/dispatches.py`

## Context

The plan lists open business questions (Q1-Q29). The maintainer answered the blocking ones on 2026-10-26 (step H0.1). An agent must not invent business rules; for a question that is still open it uses the working assumption and says so in the PR.

## Decision

### Answers (H0.1, 2026-10-26)

| # | Answer |
| --- | --- |
| Q1 | Accepted: partners request only for projects shared with them through `ProjectAssignment(kind=PARTNER)`. |
| Q2 | Accepted: the project's procurement branch buys, and its company is the buyer on the purchase order. |
| Q4 | **Changed:** every request needs approval, partner requests included. There is no per-branch `requires_approval` switch. The self-approval rule (`allow_self_approval`) is unchanged. **Amended by ADR-0014:** group administrators, material managers and engineers decide their own requests; the rule stays for the other reviewers. |
| Q5 | Accepted: the assigned site manager or the procurement branch's material manager approves; one level. |
| Q6 | Accepted: technicians request for any active project in their branch scope. |
| Q20 | **Changed:** see ADR-0013 (interim single-account setup on a public repository). |

### Working assumptions still in force

| # | Question | Assumption |
| --- | --- | --- |
| Q3 | Warehouses per branch | 0..n `WAREHOUSE` locations per branch, one default |
| Q7 | Send purchase orders from the app | **Decided (maintainer, 2026-11-16, after the pilot's first orders; amended 2026-11-16 by Q82, Q83 and Q86, M14.4): sending from the app is wanted, from the order's page.** `PO_DISPATCH` has three values: `off` (the printable page only, no button), `manual` (a buyer sends an issued order from its page, *Send to the supplier*; nothing is sent at issue or amendment) and `on` (also queued at each issue and amendment, as M5.3). Absent means `off` (fail safe); an unknown value refuses to start; the former boolean words keep their meaning (`1`/`true`/`yes`/`on` → `on`, `0`/`false`/`no`/`off` → `off`). New installations' `app.env` templates say `manual`; `init.sh` never changes an existing value. A send by hand goes to the supplier's **current** address (shown in the dialog before sending, recorded on the dispatch and in the audit), with the content still frozen from the order's issue snapshots (prices recorded on the order included), for `ISSUED` orders only, refused while a dispatch of the current revision is queued or being sent; a repeat of a revision already dispatched gets its own `Message-ID`, the first keeps the stable one. Issue and amendment under `on` still use the address the order was issued to. Under `manual` and `on`, every action that queues, withdraws or owes a supplier email says so after the call (*queued*, never *sent*), naming the address, and the order page's dispatch status refreshes itself while in flight. The cancellation notice stays owed whatever the value. No PDF |
| Q8 | Photos and attachments | Recommended; optional step M6.4 |
| Q9 | Who confirms a direct-to-site receipt | Material manager or warehouse operator, or the project's site manager through the expected-deliveries view. **Amended by [ADR-0028](0028-project-receiving-for-every-role.md) (Q44):** every business role receives at the worksite of a project in its membership's scope, whoever requested the goods |
| Q10 | Prices and discounts | Optional unit price and discount, visible only with `price.view` |
| Q11 | UI languages | Spanish by default, English source strings, French available |
| Q12 | Session length | 14 days for technicians and partners; 12 h plus MFA for privileged roles |
| Q13 | Data retention | Business documents and audit kept 6 years (to confirm with the company's accounting or legal advisor); users are deactivated, never deleted |
| Q14 | Direct-to-site split | Automatic by default (priority, date) with manual override |
| Q15 | Unit conversions | No; one base unit per material (B7) |
| Q16 | Vans as mobile warehouses | Future `Location(kind=VEHICLE)` (B5) |
| Q17 | Invoicing or accounting integration | None in the MVP; CSV exports |
| Q18 | One deployment per unrelated business group | Yes (ADR-0003) |
| Q19 | Recovery targets | RPO 0 for failed deploys; RPO <= 6 h and RTO <= 4 h for loss of the VPS |
| Q21 | Maintenance window per deploy | 1-3 minutes, outside working hours |
| Q22 | Attachment quotas | 10 MB per file, 10 files per document, 5 GB total with an alert at 80 %, 2 concurrent uploads |
| Q23 | Accounts after a restore without a preserved database | All privileged accounts stay suspended until the maintainer confirms them, plus anyone reported as offboarded or with changed credentials during the gap; recovery codes are invalidated for everyone |
| Q24 | Abandoned draft purchase orders | Not released automatically; drafts older than 14 days are flagged on the material-manager dashboard and a human discards them |
| Q25 | Who releases quarantined emails after a restore | The maintainer: notifications in bulk, supplier dispatches one by one after checking with the supplier |
| Q26 | Who may change a user's email | Administrators only (`org.manage`; superusers for staff), confirmed by the new address, old address notified |
| Q27 | Offsite append-only storage | A restic REST server in append-only mode, or an S3-compatible bucket with object lock; chosen in H8.6 |
| Q28 | Email-change cooling-off | 24 h; the old address can cancel during it |
| Q29 | First administrator's setup link | Printed once to the operator's terminal; emailing it is optional |

## Consequences

- A decision here wins over the plan text. Changing one needs a new ADR or an amendment approved by the maintainer.
- A step that depends on an assumption names it in the PR description for the reviewer.
