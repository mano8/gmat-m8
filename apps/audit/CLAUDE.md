# `apps/audit`

What this app holds and how its parts fit. Loaded when you work in this directory; the
rules that hold everywhere are in the root `CLAUDE.md`. Keep this file current when the
app changes.

## Layout

- append-only `AuditEvent` (scoped by its `branch_id`: `visible_to(ctx, audit.view)`, branchless events only for a grant that reaches everything)
- `recorder.record()`, and the viewer (M7.3, `audit.view`): `selectors` (`events` with `EventFilter`, `object_types`, `object_history`; no payload, no IP is selected), the list `/audit/` and one object's history `/audit/object/<type>/<id>/` (404 when none of its events is in scope)
- `templates/audit/`
