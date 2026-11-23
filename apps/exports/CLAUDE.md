# `apps/exports`

What this app holds and how its parts fit. Loaded when you work in this directory; the
rules that hold everywhere are in the root `CLAUDE.md`. Keep this file current when the
app changes (introduced M7.3, `export.csv`).

## Layout

- CSV downloads `/exports/` and `/exports/<key>.csv` (`requests`, `orders`, `receipts`; streamed attachments, one row per document line, drafts left out)
- `selectors` (rows through `visible_to(ctx, export.csv)`; orders from their issue snapshots; a price column only with `price.view` somewhere and a value only on the orders where it reaches)
- `cells` (`cell`/`safe_text`: every string that could start a formula is prefixed with `'`; new exports must write through `cell`)
