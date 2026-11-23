# `apps/catalog`

What this app holds and how its parts fit. Loaded when you work in this directory; the
rules that hold everywhere are in the root `CLAUDE.md`. Keep this file current when the
app changes.

## Layout

- `MaterialCategory` (two levels, trigger)
- `UnitOfMeasure` (seeded; M10.3: `is_active`, managed by group administrators on `/catalog/units/` through `services.create_unit`/`update_unit` under `org.manage`: `code` fixed and `decimals` only raised while anything uses the unit (`selectors.unit_in_use`, from the model's reverse relations), never deleted; deactivated only while `unit_usage.uses` finds nothing (active materials, and the lines in progress the document apps register with `register_unit_users`), from a lazy confirmation dialog that lists them; `unit_views`, `unit_dialogs`; an inactive unit leaves the material form, `import_catalog` and the free-text line forms, `selectors.active_units`)
- `Material` (`UNIQUE (id, unit_id)` for the C9 composite FKs)
- `catalog_fold` + trigram search in `selectors` (`search`, `materials`), the screens under `/catalog/` (`/catalog/search/` is the picker fragment, never prices)
- `manage import_catalog`

## Units of measure (Q72, ADR-0030)

Units are managed by `org.manage` (not `catalog.manage`): a unit's `code` is fixed and its `decimals` only raised once anything uses it, and it is deactivated only once no active material and no line of a request or order in progress uses it; free-text lines take only active units, and a material comes back only with its unit (`reactivate_material(with_unit=True)`, also `org.manage`: the material page's one-step dialog) (Q72, ADR-0030).

## Rules in detail

The full text of the root `CLAUDE.md`'s rules this directory implements; the root keeps a one-sentence summary of each.

- Master data (catalog, units, suppliers) is installation-wide: models use `InstallationQuerySet`, a grant reaches all of it or none, and what a role sees is decided by the capability (`catalog.view` every role, `supplier.view` only group admins, material managers, engineers and warehouse operators; suppliers never reach technicians, partners or site managers outside the receiving projection of their projects: the supplier's display name only, ADR-0028). Materials, suppliers and units of measure are deactivated, never deleted; a material's `reference` is fixed. Units of measure are managed by `org.manage`, not `catalog.manage`, and never deleted (Q72, ADR-0030; the rules in `apps/catalog/CLAUDE.md`). Imports (`manage import_*`) and `seed_demo` call the same services as the screens, as an `OperatorPrincipal` per capability; a file with any error writes nothing.
