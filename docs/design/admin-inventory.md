# What is administered where

Every model registered in the Django admin and every `manage` command, and where a person does that work
once the admin is optional and read-only ([ADR-0030](../adr/0030-administration-in-the-app.md); the
maintainer's decisions of 2026-11-12, issue #53). Taken on 2026-11-13 from `admin.site._registry` and
`manage.py help --commands` on `main` after PR #54.

Each entry is in one of four classes:

- **screen**: the app already does it; the URL names are given.
- **needs a screen**: the admin is the only place today (or nowhere); the step of M10 that adds the
  screen, with the URL names it will have.
- **internal**: nobody administers it. The rows are written by services, triggers or the worker, and
  the admin, while on, only shows them.
- **operator**: stays on the command line, with the reason.

Table 1 is a contract: M10.5 adds a test that every model registered in the admin has a row here and
that each URL name in its *In the app* column has a POST row in the access acceptance matrix
(`ROUTES`) once its step is done. Keep the model label in backticks in the first column and the URL names
in backticks in the fourth.

## 1. Models registered in the admin

"Editable" means the admin can add or change the model today; it saves through the model's services
(`ServiceBackedModelAdmin`), for whoever the admin admits and holds the capability in brackets.

| Model | Admin today | Class | In the app | Notes |
| --- | --- | --- | --- | --- |
| `access.AccessState` | read-only | internal | — | Generation counters, bumped by triggers (`access.0002`). |
| `account.EmailAddress` | read-only | screen | `org:request_email_change`, `org:revoke_email_change` | allauth's address row; an address changes only by an administrator's email change (ADR-0004, ADR-0022). |
| `accounts.EmailChangeRequest` | read-only | screen | `org:request_email_change`, `org:revoke_email_change` | On the person's page, with its delivery badge. |
| `accounts.Invitation` | read-only | screen | `org:create_invitation`, `org:resend_invitation`, `org:revoke_invitation` | `/org/invitations/`. |
| `accounts.User` | read-only | screen; the name **needs a screen (M10.4)** | `org:set_employer`, `org:offboard`, `org:rename_person` | `/org/people/`. The full name is editable nowhere today, though ADR-0029 promised it with M10: a new service and a dialog on the person's page (§3.4). Staff and superuser flags stay out of the app. |
| `attachments.Attachment` | read-only | screen | `attachments:upload_request`, `attachments:upload_receipt`, `attachments:upload_handover`, `attachments:remove` | The panel on each document. |
| `attachments.AttachmentReservation` | read-only | internal | — | An upload's lease; the worker expires it. |
| `attachments.MediaDeletion` | read-only | internal | — | The worker's purge queue. |
| `attachments.StorageQuota` | read-only | internal | — | The limit comes from `ATTACHMENT_STORAGE_LIMIT_MB`; the 80 % alert goes by mail. |
| `audit.AuditEvent` | read-only | screen | — | `/audit/` and one object's history; append-only, nothing to write. |
| `catalog.Material` | editable (`catalog.manage`) | screen | `catalog:create`, `catalog:update`, `catalog:deactivate`, `catalog:reactivate` | `/catalog/`. |
| `catalog.MaterialCategory` | editable (`catalog.manage`) | screen | `catalog:category_create`, `catalog:category_update` | `/catalog/categories/`. |
| `catalog.UnitOfMeasure` | read-only (seeded) | **needs a screen (M10.3)** | `catalog:create_unit`, `catalog:update_unit`, `catalog:deactivate_unit`, `catalog:reactivate_unit` | Q72: new services, `is_active` by an expand migration. |
| `handovers.Handover` | read-only | screen | `handovers:create`, `handovers:void` | |
| `handovers.HandoverLine` | read-only | screen | `handovers:create` | Written with its handover. |
| `locations.Location` (warehouse) | editable (`org.manage`) | **needs a screen (M10.3)** | `locations:create_warehouse`, `locations:update_warehouse`, `locations:deactivate_warehouse`, `locations:reactivate_warehouse` | `/org/warehouses/`; the default is set on the branch (`org:update_branch`). |
| `locations.Location` (worksite) | editable (`org.manage`): name, address, activity | **needs a screen (M10.3)** | `projects:create`, `locations:update_worksite`, `locations:deactivate_worksite`, `locations:reactivate_worksite` | Created with its project; the admin is the only place to rename or deactivate one today (§3.3). |
| `material_requests.MaterialRequest` | read-only | screen | `requests:start`, `requests:submit`, `requests:examine` and the review actions | |
| `material_requests.MaterialRequestLine` | read-only | screen | `requests:add_line`, `requests:update_line`, `requests:apply_edit` | |
| `mfa.Authenticator` | read-only | screen for its owner; the reset **needs a screen (M10.4)** | `org:reset_mfa` | Enrolment and removal by the owner at `/accounts/2fa/`; a lost factor today needs `manage reset_mfa` on the server. |
| `organizations.Branch` | editable (`org.manage`) | **needs a screen (M10.2)** | `org:create_branch`, `org:update_branch`, `org:set_branch_manager`, `org:set_self_approval`, `org:deactivate_branch`, `org:reactivate_branch` | Incl. `managed_by`, `allow_self_approval`, the default warehouse, activity. |
| `organizations.Company` | editable (`org.manage`) | **needs a screen (M10.2)** | `org:create_company`, `org:update_company`, `org:deactivate_company`, `org:reactivate_company` | `create_company` also makes the head-office branch. |
| `organizations.JointVentureMember` | editable (`org.manage`) | **needs a screen (M10.2)** | `org:add_venture_member`, `org:update_venture_member` | On the joint venture's page. No removal, in the admin or the services. |
| `organizations.Membership` | read-only | screen | `org:grant_membership`, `org:revoke_membership`, `org:membership_validity` | On the person's page. |
| `projects.Project` | read-only | screen | `projects:create`, `projects:update` | |
| `projects.ProjectAssignment` | read-only | screen | `projects:assign`, `projects:revoke_assignment`, `projects:assignment_validity` | |
| `purchasing.PurchaseAllocation` | read-only | screen | `purchasing:increase_allocation`, `purchasing:unallocate` | |
| `purchasing.PurchaseOrder` | read-only | screen | `purchasing:create`, `purchasing:update`, `purchasing:issue`, `purchasing:amend`, `purchasing:cancel` | |
| `purchasing.PurchaseOrderLine` | read-only | screen | `purchasing:add_lines`, `purchasing:update_line`, `purchasing:remove_line`, `purchasing:close_line` | |
| `receiving.GoodsReceipt` | read-only | screen | `receiving:post`, `receiving:void` | |
| `receiving.GoodsReceiptLine` | read-only | screen | `receiving:post` | Written with its receipt. |
| `receiving.ReceiptAttribution` | read-only | screen | `receiving:post` | Written with its receipt. |
| `suppliers.Supplier` | editable (`supplier.manage`) | screen | `suppliers:create`, `suppliers:update`, `suppliers:deactivate`, `suppliers:reactivate` | `/suppliers/`. |

Models that are not registered in the admin (the outbox, idempotency records, document sequences, a
request's as-submitted copies and revisions, notification preferences, allauth's session list) are
written only by their services and the worker. M10 changes none of them.

## 2. `manage` commands

| Command | Class | Why |
| --- | --- | --- |
| `bootstrap_admin` | operator | Runs before any account exists; makes the first group administrator and, with `--superuser`, a platform operator. |
| `reset_mfa` | operator (break-glass) | Kept for a staff or superuser account and for when no administrator can sign in. Everyday resets move to the person's page (M10.4, `org:reset_mfa`). |
| `reencrypt_mfa` | operator | Key rotation: needs the VPS's env file and a restart. |
| `post_restore_reconcile`, `confirm_account`, `outbox_quarantine` | operator | Recovery after a restore (ADR-0016, ADR-0021), run by a platform operator before the gates open. |
| `import_catalog`, `import_suppliers`, `import_projects` | operator, for now | Bulk loading at onboarding, from a file on the server; they call the same services as the screens. Every record they write can also be made one at a time in the app. An upload screen would be a new feature, with the file scanned like an attachment: **raised with the maintainer, not planned in M10**. |
| `worker`, `verify_internal` | operator | The outbox worker's process, and the deploy's readiness check (plan §9). |
| `seed_demo`, `styleguide` | operator (dev and test only) | Refused by production settings. |
| `createsuperuser` | disabled | Raises; `bootstrap_admin --superuser` instead (F76). |
| `changepassword` (Django) | **to disable (M10.5)** | It writes no audit event, so a restore cannot replay it; the runbooks only say not to use it. M10.5 replaces it with a command that refuses, as `createsuperuser` does, pointing to *Forgot password* and to offboarding with a new invitation. |
| `account_unsetmultipleprimaryemails` (allauth) | operator | A data repair for allauth's address rows; never needed while addresses change only through the email-change flow. |
| Django's other commands (`migrate`, `collectstatic`, `compilemessages`, `check`, `createcachetable`, `clearsessions`, `dbshell`, `shell`, …) | operator | Run by the image's entrypoint, the deploy or the maintainer; no administration of business data. |

## 3. The screens M10 adds

Every page and action below is `org.manage` only: out of scope 404, without the capability 403, another
method 405, session MFA (privileged). GET pages read only; each action is its own POST-only URL with an
idempotency key, answering by the dialog contract (`apps.core.responses`: `perform`, `invalid`, `go`).
Choices come from selectors. Lists page through `apps.core.listing`; a page with several lists gives each
section a `ListScope`. *Inline* and *lazy* dialogs follow [`dialog-inventory.md`](dialog-inventory.md):
inline on a bounded page, lazy on a row of a paged list (a GET-only URL that answers the fragment to
htmx and the whole page otherwise); danger dialogs open with Cancel focused.

### 3.1 Companies and joint ventures (M10.2): `/org/structure/companies/`

| URL | Name | Method | What |
| --- | --- | --- | --- |
| `companies/` | `org:companies` | GET | List: filters active/inactive and kind, search by code or name. |
| `companies/new/` | `org:new_company` | GET | Lazy dialog from the list: code, legal name, tax id, kind. |
| `companies/create/` | `org:create_company` | POST | `create_company` (also creates the head-office branch). |
| `companies/<id>/` | `org:company` | GET | Page: facts, its branches (`branches_page`), joint-venture members (`members_page`, a joint venture only), people count. |
| `companies/<id>/update/` | `org:update_company` | POST | Inline dialog on the page: legal name, tax id, kind (the service refuses a kind change while the venture has members). `code` is fixed. |
| `companies/<id>/deactivate/confirm/` | `org:confirm_deactivate_company` | GET | Lazy danger dialog with the preview (§3.6). |
| `companies/<id>/deactivate/` | `org:deactivate_company` | POST | `update_company(is_active=False)`. |
| `companies/<id>/reactivate/confirm/` | `org:confirm_reactivate_company` | GET | Lazy dialog with the preview of who regains access. |
| `companies/<id>/reactivate/` | `org:reactivate_company` | POST | `update_company(is_active=True)`. |
| `companies/<id>/members/add/` | `org:add_venture_member` | POST | Inline dialog on a joint venture's page: member company (choices from a selector), share. |
| `venture-members/<id>/edit/` | `org:edit_venture_member` | GET | Lazy dialog per member row: the share. |
| `venture-members/<id>/update/` | `org:update_venture_member` | POST | `update_joint_venture_member`. |

### 3.2 Branches (M10.2): `/org/structure/branches/`

| URL | Name | Method | What |
| --- | --- | --- | --- |
| `branches/` | `org:branches` | GET | List: filters company, active/inactive; search by code, name, city. |
| `branches/new/` | `org:new_branch` | GET | Lazy dialog from the list or a company page (`?company=`): company, code, name, city, address, managing branch, self-approval. |
| `branches/create/` | `org:create_branch` | POST | `create_branch`. |
| `branches/<id>/` | `org:branch` | GET | Page: company, managing branch, branches it manages, warehouses with the default, projects count. |
| `branches/<id>/update/` | `org:update_branch` | POST | Inline dialog: name, city, address, default warehouse (choices: the branch's active warehouses). `company` and `code` are fixed. |
| `branches/<id>/manager/preview/` | `org:preview_branch_manager` | GET | Fragment swapped into the dialog when a managing branch is chosen (`?managed_by=`): the preview (§3.6). |
| `branches/<id>/manager/` | `org:set_branch_manager` | POST | Inline danger dialog: `update_branch(managed_by_id=…)`; the candidates are the branches that no other branch manages, never the branch itself (one level), from a selector. |
| `branches/<id>/self-approval/` | `org:set_self_approval` | POST | Inline dialog naming the self-decision rule (ADR-0019) before `allow_self_approval` changes. |
| `branches/<id>/deactivate/confirm/` | `org:confirm_deactivate_branch` | GET | Lazy danger dialog with the preview. |
| `branches/<id>/deactivate/` | `org:deactivate_branch` | POST | `update_branch(is_active=False)`. |
| `branches/<id>/reactivate/confirm/` | `org:confirm_reactivate_branch` | GET | Lazy dialog with the preview. |
| `branches/<id>/reactivate/` | `org:reactivate_branch` | POST | `update_branch(is_active=True)`. |

### 3.3 Warehouses and worksites (M10.3): `/org/warehouses/`, `/org/worksites/`

Views in `apps/locations` (namespace `locations`), included under `org/`.

| URL | Name | Method | What |
| --- | --- | --- | --- |
| `warehouses/` | `locations:warehouses` | GET | List: filters branch, active/inactive. |
| `warehouses/new/` | `locations:new_warehouse` | GET | Lazy dialog from the list or a branch page (`?branch=`): branch (active ones, from a selector), name, address. |
| `warehouses/create/` | `locations:create_warehouse` | POST | `create_warehouse`. |
| `warehouses/<id>/edit/` | `locations:edit_warehouse` | GET | Lazy dialog per row: name, address. |
| `warehouses/<id>/update/` | `locations:update_warehouse` | POST | `update_location`. |
| `warehouses/<id>/deactivate/confirm/` | `locations:confirm_deactivate_warehouse` | GET | Lazy danger dialog; the service refuses the branch's default warehouse and the refusal shows there. |
| `warehouses/<id>/deactivate/` | `locations:deactivate_warehouse` | POST | `update_location(is_active=False)`. |
| `warehouses/<id>/reactivate/` | `locations:reactivate_warehouse` | POST | A button on the row: `update_location(is_active=True)`. |
| `worksites/<id>/update/` | `locations:update_worksite` | POST | Inline dialog on the project page, shown only with `org.manage`: name, address. |
| `worksites/<id>/deactivate/` | `locations:deactivate_worksite` | POST | Inline danger dialog on the project page: while inactive, the worksite is no longer offered for receipts. |
| `worksites/<id>/reactivate/` | `locations:reactivate_worksite` | POST | Inline dialog on the project page. |

### 3.4 People (M10.4): on `/org/people/<id>/`

| URL | Name | Method | What |
| --- | --- | --- | --- |
| `people/<id>/name/` | `org:rename_person` | POST | Inline dialog: the full name (required). A new `accounts.services.rename_user`: `org.manage`, the user row locked, staff and superuser accounts refused (`require_may_manage_account`), audited `user.renamed` with the old and new name. One's own name is allowed: a name is neither authority nor email (ADR-0022 holds). |
| `people/<id>/mfa-reset/` | `org:reset_mfa` | POST | Inline danger dialog, shown only when the person has a factor, never on one's own page or a staff account's: the consequences, a required reason, a required tick "I checked this person's identity outside the application". `reset_user_mfa` gains a keyword `identity_checked: bool` (refused when false) stored in the audit payload. |

### 3.5 Units of measure (M10.3): `/catalog/units/`

| URL | Name | Method | What |
| --- | --- | --- | --- |
| `units/` | `catalog:units` | GET | List: active/inactive, search by code or name; how many materials use each unit. |
| `units/new/` | `catalog:new_unit` | GET | Lazy dialog: code, name, decimals. |
| `units/create/` | `catalog:create_unit` | POST | `create_unit`. |
| `units/<id>/edit/` | `catalog:edit_unit` | GET | Lazy dialog per row; `code` and a lower `decimals` are disabled once a material uses the unit (the service decides). |
| `units/<id>/update/` | `catalog:update_unit` | POST | `update_unit`. |
| `units/<id>/deactivate/` | `catalog:deactivate_unit` | POST | A button on the row; existing materials keep the unit. |
| `units/<id>/reactivate/` | `catalog:reactivate_unit` | POST | A button on the row. |

### 3.6 The preview of a scope-defining change

Deactivating or reactivating a company or a branch, and changing a branch's managing branch, change
who reaches what. Their dialog says it before the confirmation, from a selector
(`organizations.selectors.scope_change_preview`), as counts and names:

- the branches that leave or join the managed scope (managing branch changes);
- the active memberships and project assignments it touches, and the number of people who lose (or
  regain) all access because every grant they hold is in that scope;
- the open documents that make the service refuse (deactivation providers), so the dialog can say so
  before the attempt.

The preview is a reading, not a promise: the service checks its preconditions again inside its
transaction, under `AccessState` `FOR UPDATE`, and a refusal comes back into the dialog (409 or 422) with
the service's reason.

### 3.7 Navigation

A new category **Administration** (key `administration`, added to `apps.core.preferences.CATEGORY_KEYS`
so it can be folded; ADR-0027) shown only with `org.manage`: People, Companies, Branches, Warehouses,
Units. *People* moves there from *Organization*, so every `org.manage`-only entry sits together; nobody
without `org.manage` sees a change. The entries rank after *Exports* in the phone bar, so they stay in
*More*. *People* is active on `org:people`, `org:person` and the invitation pages only, now that `/org/`
holds the structure pages too, and *Catalog* is not active on `/catalog/units/`.

## 4. What M10.5 builds against this inventory

- **Every model read-only.** `ServiceBackedModelAdmin` loses `may_edit`, `save_via_service`,
  `form_values`, `adopt` and the `changeform_view` override; its add and change permissions are false;
  `CapabilityModelAdmin`, `MasterDataAdmin` and `CatalogDataAdmin` go, and each `ModelAdmin` above
  becomes a `ReadOnlyModelAdmin`.
- **System check `gmat.E024`**, next to `gmat.E005`: every registered `ModelAdmin`, and every inline it
  declares, is a `ReadOnlyModelAdmin` (or a read-only inline base) whose `has_add_permission`,
  `has_change_permission`, `save_model`, `save_related`, `save_formset` and `get_actions` are the base's own
  methods, not overrides. Comparing the methods themselves needs no request and catches a subclass
  that turns editing back on. It runs with the admin off too: the admin app stays installed and its
  models registered.
- **Access-rule check** (`scripts/check_access_rules.py`): a class in an `admin.py` that defines one of
  those methods, or is built on anything but `ReadOnlyModelAdmin`, fails.
- **Inventory test** (`tests/access/test_admin_inventory.py`): parses table 1; every model in
  `admin.site._registry` has a row; every URL name in the *In the app* column resolves and has a POST row
  in `ROUTES`; no row says *needs a screen* once M10.6 is done.
- **`changepassword` disabled** (§2).
