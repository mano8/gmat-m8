# `apps/projects`

What this app holds and how its parts fit. Loaded when you work in this directory; the
rules that hold everywhere are in the root `CLAUDE.md`. Keep this file current when the
app changes.

## Layout

- `Project`
- `ProjectAssignment`, the project screens under `/projects/` (`views`, `forms`, `selectors`)
- `manage import_projects`
- `procurement` (followers of a change of procurement branch, registered from `AppConfig.ready`: `material_requests.procurement` moves the project's open requests along, or refuses while one is allocated, received or delivered; a closed request reopened by hand catches up then if nothing is committed)
