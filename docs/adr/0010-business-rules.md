# ADR-0010: Business rules: H0.1 answers and working assumptions

- **Status:** Accepted
- **Date:** 2026-10-26

## Context

The plan lists open business questions (Q1-Q29). The maintainer answered the blocking ones on 2026-10-26 (step H0.1). An agent must not invent business rules; for a question that is still open it uses the working assumption and says so in the PR.

## Decision

### Answers (H0.1, 2026-10-26)

| # | Answer |
| --- | --- |
| Q1 | Accepted: partners request only for projects shared with them through `ProjectAssignment(kind=PARTNER)`. |
| Q2 | Accepted: the project's procurement branch buys, and its company is the buyer on the purchase order. |
| Q4 | **Changed:** every request needs approval, partner requests included. There is no per-branch `requires_approval` switch. The self-approval rule (`allow_self_approval`) is unchanged. |
| Q5 | Accepted: the assigned site manager or the procurement branch's material manager approves; one level. |
| Q6 | Accepted: technicians request for any active project in their branch scope. |
| Q20 | **Changed:** see ADR-0013 (interim single-account setup on a public repository). |

### Working assumptions still in force

| # | Question | Assumption |
| --- | --- | --- |
| Q3 | Warehouses per branch | 0..n `WAREHOUSE` locations per branch, one default |
| Q7 | Send purchase orders from the app | No in the MVP: a printable purchase-order page |
| Q8 | Photos and attachments | Recommended; optional step M6.4 |
| Q9 | Who confirms a direct-to-site receipt | Material manager or warehouse operator, or the project's site manager through the expected-deliveries view |
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
