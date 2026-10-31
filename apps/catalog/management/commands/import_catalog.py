"""``manage import_catalog <file> --operator <email> [--dry-run]`` (plan M3.3).

Upserts materials by ``reference`` (ignoring case) from a CSV or XLSX file, through the
catalog services; see ``apps.core.imports`` for the run and ``docs/import-templates/``
for the columns. Categories named in the file are created when missing (``Cables > Unipolares``
is the subcategory ``Unipolares`` of ``Cables``). A column that is in the file sets that
field, a blank text cell clearing it; the yes/no columns leave the field alone when blank.
"""

import re
from dataclasses import dataclass
from typing import Any, ClassVar

from apps.access.context import AccessContext
from apps.access.principals import OperatorPrincipal
from apps.access.roles import Capability
from apps.catalog import selectors, services
from apps.catalog.models import EAN_PATTERN, Material, MaterialCategory, UnitOfMeasure
from apps.core.imports import (
    CREATED,
    UNCHANGED,
    UPDATED,
    ImportCommand,
    RowRefused,
    parse_bool,
    require,
)
from apps.core.tabular import Column, Row

COLUMNS = (
    Column("reference", ("referencia", "ref", "codigo", "code", "sku"), required=True),
    Column("name", ("nombre", "designacion", "designation", "nom"), required=True),
    Column("unit", ("unidad", "ud", "unite", "uom"), required=True),
    Column("category", ("categoria", "familia", "categorie")),
    Column("description", ("descripcion", "detalle")),
    Column("manufacturer", ("fabricante", "marca", "brand", "fabricant")),
    Column(
        "manufacturer_ref",
        (
            "manufacturer_reference",
            "referencia_fabricante",
            "referencia_del_fabricante",
            "ref_fabricante",
            "mpn",
        ),
    ),
    Column("ean", ("ean13", "barcode", "codigo_de_barras", "gtin", "upc")),
    Column("is_stock_item", ("stock_item", "stock", "en_stock", "articulo_de_almacen")),
    Column("active", ("is_active", "activo", "actif")),
)
CATEGORY_SEPARATOR = ">"
MAX_CATEGORY_LEVELS = 2


@dataclass(frozen=True)
class MaterialRow:
    reference: str
    values: dict[str, Any]  # the material fields the file carries, ready for the service
    unit_code: str
    category_path: tuple[str, ...] | None  # None: no category column; (): a blank cell
    stock: bool | None
    active: bool | None


class Command(ImportCommand):
    help = "Import or update the material catalog from a CSV or XLSX file."
    required_capability: ClassVar[str] = Capability.CATALOG_MANAGE
    columns = COLUMNS
    audit_action = "catalog.imported"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.ctx: AccessContext
        self.units: dict[str, UnitOfMeasure] = {}
        self.categories_created = 0

    def natural_keys(self, parsed: MaterialRow) -> list[str]:
        return [parsed.reference.casefold()]

    def parse(self, row: Row) -> MaterialRow:
        reference = require(row, "reference")
        name = require(row, "name")
        unit = require(row, "unit")
        values: dict[str, Any] = {"name": name}
        if len(reference) > 60:
            raise RowRefused("is longer than 60 characters", "reference")
        if len(name) > 200:
            raise RowRefused("is longer than 200 characters", "name")
        for key, limit in (("manufacturer", 100), ("manufacturer_ref", 100)):
            if key in row.values:
                if len(row.get(key)) > limit:
                    raise RowRefused(f"is longer than {limit} characters", key)
                values[key] = row.get(key)
        if "description" in row.values:
            values["description"] = row.get("description")
        if "ean" in row.values:
            ean = row.get("ean")
            if ean and not re.fullmatch(EAN_PATTERN, ean):
                raise RowRefused(f"{ean!r} is not 8 to 14 digits", "ean")
            values["ean"] = ean or None
        category_path = None
        if "category" in row.values:
            parts = tuple(p.strip() for p in row.get("category").split(CATEGORY_SEPARATOR))
            category_path = () if parts == ("",) else parts
            if any(not part for part in category_path):
                raise RowRefused("has an empty level", "category")
            if len(category_path) > MAX_CATEGORY_LEVELS:
                raise RowRefused(f"has more than {MAX_CATEGORY_LEVELS} levels", "category")
        stock = active = None
        if row.get("is_stock_item"):
            stock = parse_bool(row.get("is_stock_item"), "is_stock_item")
        if row.get("active"):
            active = parse_bool(row.get("active"), "active")
        return MaterialRow(reference, values, unit, category_path, stock, active)

    def begin(self, principal: OperatorPrincipal, ctx: AccessContext) -> None:
        self.ctx = ctx
        self.units = selectors.units_by_code(ctx)
        self.categories_created = 0

    def summary(self) -> dict[str, Any]:
        return {"categories_created": self.categories_created}

    def apply(self, principal: OperatorPrincipal, parsed: MaterialRow) -> str:
        unit = self.units.get(parsed.unit_code.upper())
        if unit is None:
            raise RowRefused(
                f"unknown unit {parsed.unit_code!r} (use one of {', '.join(sorted(self.units))})",
                "unit",
            )
        fields = dict(parsed.values, unit_id=unit.pk)
        if parsed.category_path is not None:
            fields["category_id"] = self._category(principal, parsed.category_path)
        existing = selectors.material_by_reference(self.ctx, parsed.reference)
        if existing is None:
            material = services.create_material(
                principal,
                reference=parsed.reference,
                is_stock_item=bool(parsed.stock),
                **fields,
            )
            if parsed.active is False:
                services.deactivate_material(principal, material.pk)
            return CREATED
        return self._update(principal, existing, fields, parsed)

    def _update(
        self,
        principal: OperatorPrincipal,
        existing: Material,
        fields: dict[str, Any],
        parsed: MaterialRow,
    ) -> str:
        changes = {key: value for key, value in fields.items() if getattr(existing, key) != value}
        if parsed.stock is not None and parsed.stock != existing.is_stock_item:
            changes["is_stock_item"] = parsed.stock
        changed = bool(changes)
        if changes:
            services.update_material(principal, existing.pk, **changes)
        if parsed.active is not None and parsed.active != existing.is_active:
            action = services.reactivate_material if parsed.active else services.deactivate_material
            action(principal, existing.pk)
            changed = True
        return UPDATED if changed else UNCHANGED

    def _category(self, principal: OperatorPrincipal, path: tuple[str, ...]) -> Any:
        """The id of the category at ``path``, creating the missing levels."""
        parent: MaterialCategory | None = None
        for name in path:
            found = selectors.category_named(self.ctx, name, parent.pk if parent else None)
            if found is None:
                found = services.create_category(
                    principal, name=name, parent_id=parent.pk if parent else None
                )
                self.categories_created += 1
            parent = found
        return parent.pk if parent is not None else None
