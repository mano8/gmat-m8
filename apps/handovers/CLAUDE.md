# `apps/handovers`

What this app holds and how its parts fit. Loaded when you work in this directory; the
rules that hold everywhere are in the root `CLAUDE.md`. Keep this file current when the
app changes.

## Layout

- `Handover` (`FROM_WAREHOUSE` / `DIRECT_RECEIPT`, number `ENT-…` per branch) and `HandoverLine` (C3 composite keys, one line per receipt attribution), a trigger freezing posted handovers except their void (`handovers.0002`)
- `transitions`
- `common` (numbers `ENT-…`, audit and events of both kinds)
- `direct` (direct handovers created and voided only with their receipt)
- `ledger` (the D flow source: posted handovers of both kinds)
- `services` (M6.2, `FROM_WAREHOUSE` only: `create_handover` authorized with `handover.post` on the requests served (C8: their procurement branch is the handover's), component locked L2-L5, S9 project, C7 source warehouse, each line open and ≤ A - D (I5), the receiver a user of the project or a typed name, number at L9, automatic close, reconcile (pending above the need is cut); `void_handover` refuses `DIRECT_RECEIPT`, L2-L5 then L7, reopen, reconcile)
- `selectors` (`warehouse_stock`, `stock_warnings`: what a warehouse received minus handed over, a warning only (I9); M6.3: handovers and their lines, `posting_projects`/`posting_project`/`posting_request`, `open_lines` (A - D), `sources` (C7), `receivers`), read-only admin, and the screens under `/handovers/` (M6.3: `views.py`, `forms` (`HandoverForm` with `Choices` from selectors), `urls`, `templates/handovers/`): list `/handovers/` (`handover.view`, a site manager reads their project's), page `<id>/` (void dialog for a warehouse handover; a site one points to its receipt)
- `new/?project=|request=` (`handover.post`) → `create/` (idempotent; a shortfall against the known stock comes back once as a warning with a box to tick)
- `<id>/void/`
