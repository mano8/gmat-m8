# `apps/locations`

What this app holds and how its parts fit. Loaded when you work in this directory; the
rules that hold everywhere are in the root `CLAUDE.md`. Keep this file current when the
app changes (introduced M10.3).

## Layout

- `Location`
- `services` (`create_warehouse`, `update_location`: the branch's default warehouse cannot be deactivated)
- `selectors` (`warehouses`, `warehouse`, `worksite`, `worksite_of`)
- `views`/`forms`/`dialogs`/`urls` under `/org/warehouses/` (the list, lazy dialogs for new, edit and deactivate, a button to reactivate) and `/org/worksites/<id>/` (POST-only update, deactivate, reactivate, from inline dialogs on the project page for `org.manage` holders; `dialogs.worksite_dialogs`)
- `templates/locations/`
