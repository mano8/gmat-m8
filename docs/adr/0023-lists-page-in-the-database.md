# ADR-0023: Lists page in the database; lists worked out from the ledger use a cursor

- **Status:** Accepted (the maintainer's request for M7d, issue #27; implemented in PR #31)
- **Date:** 2026-11-08 (recorded); merged 2026-11-03
- **Governs:** `apps/core/listing.py`, `tests/integration/test_list_volume.py`, `tests/volume.py`

## Context

Issue #27 asked for filters, page sizes and navigation on every list. The planning notes (Q40) chose offset pagination with an exact count, and keyset navigation only for a list that missed the time budget. Some lists cannot be paged that way. Their rows are worked out from the quantity ledger (ADR-0005): the purchasing worklist and its *ordered for nobody* section, a project's materials, and a project's expected deliveries. Counting them or reaching page *n* means working out the whole backlog first.

## Decision

- **Every list pages in the database** through `apps.core.listing` (`paginate`, `render_list`): `LIMIT` in SQL, never Python after loading the whole set. Page, page size (10, 25, 50, 100) and sort live in the URL only. Sorting is allowlisted per list, with the primary key as the tie-breaker, and an invalid value falls back to the default. Bounded document lists are the exceptions (Q43): the lines of one document, the attachments panel, dashboard panels and the catalog picker.
- **A list whose rows are worked out from the ledger uses a cursor** (`cursor_page`, `CursorPage`, `?after=` and `?back=`). Its source reads the next candidate keys in the list's order from the database (`seek_after`), and its window works out only those rows. Its order and filters are SQL-able: a derived quantity may drop rows in the window, never order them. Navigation is First / Previous / Next, with no total. Dashboard counts built the same way stop at "99+" (`head`).
- **Budget:** every list answers in at most 300 ms of server time on the volume dataset (`tests/volume.py`) and costs the same number of queries at 10 and at 100 rows (`tests/integration/test_list_volume.py`). A list of heavy cards caps its page size (`ListScope(max_per_page=…)`).
- A page with several lists gives each its own prefixed parameters (`ListScope`).

## Consequences

- This replaces the planning notes' Q40: offset paging stays for lists of stored rows, and the cursor is the rule for lists worked out from the ledger, not an exception.
- A new list gets its `Screen` in the volume test, and a cursor list proves that it gives, page after page, the rows of the eager selector it replaces.
- `CLAUDE.md` (Conventions, Lists) keeps the working detail for contributors.
