# `apps/dashboards`

What this app holds and how its parts fit. Loaded when you work in this directory; the
rules that hold everywhere are in the root `CLAUDE.md`. Keep this file current when the
app changes (introduced M7.2).

## Layout

- `panels` (the registry `PANELS`: a capability, a title and a `build` per panel, reading only through the owning apps' scoped selectors, e.g. `material_requests.selectors.sourcing_to_decide`/`closed_needing_attention`, `material_requests.selectors.assigned_to` (the `assigned` panel, M7b.4: submitted, approved and rejected requests sent to the user (`assigned_to(ctx, statuses=)`), for `request.edit` holders; its link is the inbox filtered with `?assigned=1`), `purchasing.selectors.awaiting_receipt`/`overdue`/`dispatches_needing_attention`, `handovers.selectors.awaiting_handover`, `receiving.selectors.site_surplus`; a count plus the first `LIMIT` rows, never a price)
- `boards` (M14.3: `Board.HOME`/`ADMINISTRATION`, one per panel, `HOME` by default; `panels.visible(ctx, board)`; the organisation's `overview`, `branch-people` and `branch-requests` are `ADMINISTRATION`, each read with `org.manage`, a test keeps it so; `dispatches` stays on home)
- `views` (`home` at `/` lists the day-to-day placeholders the user's capabilities reach, each loaded with `hx-trigger="load"` from `panel` at `/dashboard/panels/<key>/`: GET only, 403 without the panel's capability, 404 for an unknown key; `administration` at `/dashboard/administration/` (`org.manage`, GET and HEAD, the first entry of the Administration menu) lists the administration board's panels with the period selector and no key figures; both pages draw the shared includes `_board.html`, `_period.html` and `_chart_scripts.html`)
- `templates/dashboards/`
- M8b.5: the chart panels in the same registry (`chart` (`Chart`, `Series`, `Meter`, `ChartPanel` with `requires`: offered only to who holds every capability it reads with; counts and durations only), `charts` (`CHARTS`: decisions, inbox by age, orders and receipts, on-time receipts, overdue by supplier (`supplier.view`), receipts and handovers, awaiting handover by project, requests and people per branch (`org.manage`), a requester's approved-and-delivered meters), each reading the owning apps' aggregate selectors (`material_requests.selectors.trends`, `purchasing.selectors.trends`, `receiving.selectors.posted_by_week`/`on_time_by_week`, `handovers.selectors.posted_by_week`/`awaiting_by_project` (the ledger's awaiting handover in SQL), `organizations.selectors.people_per_branch`/`branch_labels`))
- `tiles` (at most four ranked key figures for the user's capabilities, `dashboard:tiles` at `/dashboard/tiles/`, "99+" through `head`)
- `period` (`?weeks=4|12|26`, 12 by default, in the URL only)
- the chart's data travels as JSON in the canvas's `data-chart` attribute (htmx strips every `<script>` from a swapped fragment, so never `json_script` in a panel), drawn by `static/js/charts.js` (loaded with Chart.js only on a page that draws a chart
