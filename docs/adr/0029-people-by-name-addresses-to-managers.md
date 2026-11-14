# ADR-0029: People are shown by name; an address only where managing them needs it

- **Status:** Proposed (the maintainer's decision in the review of the M9 pull request, #54, 2026-11-13; accepted when that PR merges)
- **Date:** 2026-11-13
- **Governs:** `apps/accounts/people.py`, `apps/accounts/templatetags/people.py`, `apps/accounts/management/commands/bootstrap_admin.py`, `apps/accounts/services/invitations.py`, `apps/organizations/forms.py`, `apps/material_requests/selectors/reviewer.py`

## Context

The screens showed a person as `full_name|default:email`: the name, or the address when the account had none. A name was optional when inviting, and the first administrator (`manage bootstrap_admin`) had none. Before M9 this reached only staff of the same branches. Since M9 (ADR-0028), technicians and partners read the worksite receipts of their projects, with who received, voided or added a file. A partner works for another company, so a staff member without a name showed them their address.

## Decision

1. **Everyone is named.** A full name is required to invite someone (`InvitationForm`, and `create_invitation` itself) and to create the first administrator (`manage bootstrap_admin --full-name`). Resending an earlier invitation keeps the name it had. An active account without a name takes the name of an invitation it accepts; a named account keeps its own.
2. **Pages show people by name.** `apps.accounts.people.name_of` and the `person` filter (`{% load people %}`) give the full name, or "—" for an account created before names were required. They never give the address. They are used on every page, fragment, picker and notification mail that shows another person: receipts, handovers, attached files, a request's status page, history, ledger and edits, a project's assignments, the reviewer and receiver pickers, the "submitted" and "assigned" mails.
3. **An address only where managing people needs it.**
   - Group administrators see addresses on the people and invitation screens (`/org/`).
   - On the review page, the requester's address is shown to who reviews the request through a managing role: group administrators, material managers, engineers (`selectors.shows_requester_address`, the `person_contact` filter). Site managers also hold `request.review`, for their projects: they review by name and never see the address.
   - Everyone sees their own address (their account menu and pages).
   - Warehouse operators, site managers, technicians and partners never see another person's address.
4. **Kept as they are, by the maintainer's choice or because they record history.**
   - The requests CSV export keeps the requester's address (maintainer, 2026-11-13); `export.csv` belongs to the managing roles.
   - The audit viewer shows the actor label the audit log recorded at the time, an address, to `audit.view` holders (group administrators, material managers, engineers); the log is append-only.
   - The Django admin shows addresses to its admitted users (group administrators and platform operators).
5. **Accounts without a name** show "—" until an administrator names them. *Delivered in M10.4 (ADR-0030, point 6):* a group administrator sets or corrects a person's full name on the person's page (`org:rename_person`, `accounts.services.rename_user`).

## Consequences

- A test reads every URL of the acceptance matrix as each actor who is not a group administrator, material manager or engineer and fails on another fixture person's address (`tests/access/test_people_addresses.py`).
- The pickers list people by name, in name order. Two people with the same name, or two older accounts without one ("—"), look alike there. Names are required from now on, no installation has been released before this decision, and M10 lets an administrator name an account; until then the people screens tell them apart.
- The "submitted" mail to reviewers names the requester without the address, because its recipients include site managers.
- `manage bootstrap_admin` needs `--full-name`; the runbooks and READMEs say so.
