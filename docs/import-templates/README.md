# Import templates

Three commands load master data from a spreadsheet. They run on the server, as a user who
holds the matching capability, and go through the same services as the screens, so every
rule of the app applies and every change is audited.

```bash
docker compose run --rm -v "$PWD/imports:/imports:ro" web manage import_catalog   /imports/catalog.csv   --operator you@example.com --dry-run
docker compose run --rm -v "$PWD/imports:/imports:ro" web manage import_suppliers /imports/suppliers.csv --operator you@example.com
docker compose run --rm -v "$PWD/imports:/imports:ro" web manage import_projects  /imports/projects.csv  --operator you@example.com
```

| Command | Capability the operator needs | A row is matched by |
| --- | --- | --- |
| `import_catalog` | `catalog.manage` | `reference`, ignoring case |
| `import_suppliers` | `supplier.manage` | `tax_id`, else `name` (ignoring case) |
| `import_projects` | `project.manage` | `code` |

## How an import works

- **Formats.** `.csv` (UTF-8, or Windows-1252 as Excel writes it; the delimiter `;`, `,` or a
  tab is taken from the header line, or give `--delimiter`) and `.xlsx` (the first sheet, or
  `--sheet NAME`; formulas are read as their last saved value). The first row is the header.
  Keep references and tax ids as *text* cells in a spreadsheet, or `007` arrives as `7`.
- **Columns** are matched by name, ignoring case and accents; Spanish and English names work
  (`referencia`, `nombre`, `unidad`…). A column the command does not know is ignored and
  listed in the report; a missing required column stops the import.
- **One transaction per file.** If any row is wrong, **nothing is written**, and the report
  lists every error with its line number. `--dry-run` runs everything and rolls it back, so
  you can check a file before loading it.
- **Idempotent.** Importing the same file again changes nothing: every row is reported as
  `unchanged`, and no record is touched.
- **Blank cells.** For a column that is in the file, a blank text cell clears the field. A blank
  yes/no cell, and a blank `procurement_branch`, `status` or `tax_id`, leave the field alone.
  A column that is not in the file is never touched.
- **Audit.** A successful import writes one audit event (`catalog.imported`,
  `supplier.imported`, `project.imported`; `via=cli:<command>`) with the file name, its SHA-256
  and the counts, besides the events of the records themselves.
- Limits: 20 MB and 100,000 rows per file.

Yes/no values: `yes`, `true`, `1`, `x`, `sí`, `oui` and `no`, `false`, `0`, `non`.

## `catalog.csv`

| Column | Required | Notes |
| --- | --- | --- |
| `reference` | yes | Your own code, at most 60 characters. Fixed once created |
| `name` | yes | At most 200 characters |
| `unit` | yes | A unit code: `UD`, `PAR`, `M`, `M2`, `KG`, `L`, `ROLLO`, `CAJA`, `BOLSA`, `BOTE`, `JUEGO` |
| `category` | no | `Parent > Child` (two levels at most). Missing categories are created |
| `description`, `manufacturer`, `manufacturer_ref` | no | |
| `ean` | no | 8 to 14 digits |
| `is_stock_item` | no | Kept in the warehouse, so it can be handed over from stock |
| `active` | no | `no` deactivates the material (it is never deleted) |

A material's unit cannot change once a request or an order uses it.

## `suppliers.csv`

| Column | Required | Notes |
| --- | --- | --- |
| `name` | yes | Unique ignoring case |
| `tax_id` | no | Unique. A supplier known by name without one gains it |
| `contact_name`, `email`, `phone`, `address`, `notes` | no | |
| `active` | no | `no` deactivates the supplier |

## `projects.csv`

| Column | Required | Notes |
| --- | --- | --- |
| `code` | yes | Upper-case letters, digits, `-` and `_`. Fixed once created |
| `name` | yes | |
| `branch` | yes | `COMPANY-BRANCH`, for example `A-MAD`: the branch that executes and bills. It must be active and within the operator's `project.manage` scope |
| `procurement_branch` | no | The branch that buys. Blank: the branch that manages `branch`, or `branch` itself |
| `client_name`, `address` | no | |
| `status` | no | `ACTIVE`, `ON_HOLD` or `CLOSED` |
| `start_date`, `end_date` | no | `YYYY-MM-DD` or `DD/MM/YYYY` |

A new project gets its worksite location automatically.
