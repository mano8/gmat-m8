# Where actions open

Every form that sat in a fold-out section (`<details>`) or on a page of its own reached from a
document, and how it opens now (M8b.4, [ADR-0027](../adr/0027-ui-design-system.md)). Behaviour,
capabilities, scopes and URLs of the actions are unchanged: every action keeps its POST-only URL, and
sent from a dialog it answers by the contract of `apps/core/responses.py`.

**Inline** dialogs are drawn in the page (a bounded page: a document and its lines). **Lazy** dialogs
are one shared dialog per kind of action on a paged list; the row's opener is a link to a GET-only URL
(`hx-get` into the dialog; the whole page without JavaScript). Destructive ones state what they do and
open with Cancel focused.

| Page | Action | Before | Now |
| --- | --- | --- | --- |
| Request review | Send to another reviewer | `<details>` | inline dialog, button by the assignee |
| | Return for changes | `<details>` | inline dialog (destructive), page menu |
| | Close, reopen, cancel the request | `<details>` cards | inline dialogs (close and cancel destructive), page menu |
| | Reduce the approved quantity | `<details>` per line | inline dialog per line, line menu |
| | Close the line short, remove the line | `<details>` per line | inline dialogs (destructive), line menu |
| | Reopen the line | `<details>` per line | inline dialog, line menu |
| | Change the line, add a line | separate page + lazy pilot | lazy dialog (M8b.3), unchanged |
| | Decide a line, sourcing, schedule | inline forms | stay inline (the page's own work, not nested) |
| | Approve all | form | sticky action bar |
| Order (draft) | Edit supplier, destination, notes | `<details>` | inline dialog, page menu |
| | Add request lines to buy | `<details>` | inline dialog (a tick and a quantity per candidate) |
| | Edit the line, remove the line | `<details>` per line | inline dialogs per line (remove destructive) |
| | Raise, lower an allocation | `<details>` per allocation | inline dialogs per allocation |
| | Discard the draft | `<details>` | inline dialog (destructive), page menu |
| | Issue the order | form | sticky action bar |
| Order (issued) | Cancel the order | `<details>` | inline dialog (destructive), page menu |
| | Close the line short | `<details>` per line | inline dialog (destructive) |
| | Send again, the supplier has it | `<details>` per dispatch | inline dialogs per dispatch |
| | Amend | separate page | stays a page (a long form of its own) |
| Receipt | Void | pilot (M8b.3) | inline dialog, unchanged |
| Receive form | Rejected goods or an incident; split between requests | `<details>` | **stay disclosures**: they hold fields of the one form that is posted (a dialog would nest a form in a form) |
| | Post the receipt | form | sticky action bar |
| Handover | Void | `<details>` | inline dialog (destructive), page menu |
| | Hand the material over | form | sticky action bar |
| Person | Grant a role | `<details>` | inline dialog |
| | Change a role's validity | `<details>` per row of a paged list | lazy dialog (new GET URL `edit_membership_validity`) |
| | Revoke a role | form per row | lazy dialog, destructive (new GET URL `confirm_revoke_membership`) |
| | Offboard | `<details>` | inline dialog (destructive), page action |
| | Revoke an email change | form | inline dialog (destructive) |
| Project | Share the project | form | inline dialog |
| | Change an assignment's validity | `<details>` per row of a paged list | lazy dialog (`edit_assignment_validity`) |
| | Revoke an assignment | form per row | lazy dialog, destructive (`confirm_revoke_assignment`) |
| Supplier, material | Deactivate | form | inline dialog (destructive) |
| Attachments panel | Remove an attachment | `<details>` per file | inline dialog per file (a moderator's reason is required) |
| Draft request | Not in the catalog? | `<details>` | inline dialog |
| | Discard the draft | form | inline dialog (destructive) |
| My requests (list) | The lines of a request | `<details>` | stays: read-only detail |
| Shell | Menu categories | `<details>` | stays: navigation, not a form |

Lists: the filters open in a sheet on a phone and show as chips (`list_toolbar`, `list_summary`);
parameters and URLs are unchanged. The new-request flow has a stepper and its submit in the sticky
bar.
