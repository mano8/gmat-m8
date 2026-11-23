# `apps/organizations`

What this app holds and how its parts fit. Loaded when you work in this directory; the
rules that hold everywhere are in the root `CLAUDE.md`. Keep this file current when the
app changes.

## Layout

- `Company`
- `JointVentureMember`
- `Branch`
- `Membership`, deactivation preconditions
- `grants` (membership target rules, privilege elevation)
- `views/` (a package: `people_screens` the people/membership/invitation screens under `/org/`; M10.4's `account_actions`: the person page's inline dialogs `org:rename_person` and `org:reset_mfa` (danger; offered only while `accounts.selectors.has_second_factor`, never on one's own page or a staff account's); M10.2's `company_screens` and `branch_screens` under `/org/structure/`: company and branch lists and pages, create, edit, joint-venture members, the managing branch (its preview refreshed by `preview_branch_manager` as the choice changes), self-approval (the value to set, never a toggle), deactivate and reactivate through lazy danger dialogs; `structure_common`)
- `structure_dialogs` (their `ActionDialog`s)
- `selectors/` (a package: `directory`, `structure`, and `preview`: `company_activity`/`branch_activity`/`branch_manager` re-read S1/S4/S8/S9 on the organization as it is and as it would be (branches leaving, joining, stopping; roles and project shares whose reach changes; who no longer reaches any branch or project; the open documents), with the `org_generation` it was read at, which the dialog posts back as `seen_generation` and `update_company`/`update_branch` refuse once the organization changed (`edited`, 409; ADR-0030))
- `forms`, with the delivery badge of each pending invitation and open email change (M7c.1: GET-only fragments `invitations/<id>/delivery/` and `email-changes/<id>/delivery/`, polled every 5 s while the message is queued or sending, every 60 s once delayed; `data-poll` in `app.js` stops a poll that is redirected or fails; flashes say "queued", never "sent")
