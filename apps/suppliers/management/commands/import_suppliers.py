"""``manage import_suppliers <file> --operator <email> [--dry-run]`` (plan M3.3).

Upserts suppliers from a CSV or XLSX file, through the supplier services; see
``apps.core.imports`` for the run and ``docs/import-templates/`` for the columns.

A row is matched by its ``tax_id`` when it has one, else by its ``name`` (ignoring case).
A supplier already known by name without a tax id gains the file's tax id. A blank text
cell clears the field, except ``tax_id``: a blank one never removes a tax id.
"""

from dataclasses import dataclass
from typing import Any, ClassVar

from apps.access.context import AccessContext
from apps.access.principals import OperatorPrincipal
from apps.access.roles import Capability
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
from apps.suppliers import selectors, services

COLUMNS = (
    Column("name", ("nombre", "proveedor", "supplier", "razon_social", "nom"), required=True),
    Column("tax_id", ("nif", "cif", "nif_cif", "vat", "vat_id", "numero_fiscal", "siret")),
    Column("contact_name", ("contact", "contacto", "persona_de_contacto", "contact_person")),
    Column("email", ("correo", "e_mail", "mail", "courriel")),
    Column("phone", ("telefono", "tel", "telephone", "mobile", "movil")),
    Column("address", ("direccion", "adresse")),
    Column("notes", ("notas", "observaciones", "comments")),
    Column("active", ("is_active", "activo", "actif")),
)
TEXT_LIMITS = {"name": 200, "tax_id": 32, "contact_name": 200, "phone": 40}
TEXT_KEYS = ("contact_name", "email", "phone", "address", "notes")


@dataclass(frozen=True)
class SupplierRow:
    name: str
    tax_id: str | None
    values: dict[str, Any]
    active: bool | None


class Command(ImportCommand):
    help = "Import or update suppliers from a CSV or XLSX file."
    required_capability: ClassVar[str] = Capability.SUPPLIER_MANAGE
    columns = COLUMNS
    audit_action = "supplier.imported"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.ctx: AccessContext

    def natural_keys(self, parsed: SupplierRow) -> list[str]:
        keys = [f"name:{parsed.name.casefold()}"]
        if parsed.tax_id:
            keys.append(f"tax:{parsed.tax_id.casefold()}")
        return keys

    def parse(self, row: Row) -> SupplierRow:
        name = require(row, "name")
        values: dict[str, Any] = {}
        for key, limit in TEXT_LIMITS.items():
            if len(row.get(key)) > limit:
                raise RowRefused(f"is longer than {limit} characters", key)
        for key in TEXT_KEYS:
            if key in row.values:
                values[key] = row.get(key)
        if values.get("email") and "@" not in values["email"]:
            raise RowRefused(f"{values['email']!r} is not an email address", "email")
        active = parse_bool(row.get("active"), "active") if row.get("active") else None
        return SupplierRow(name, row.get("tax_id") or None, values, active)

    def begin(self, principal: OperatorPrincipal, ctx: AccessContext) -> None:
        self.ctx = ctx

    def apply(self, principal: OperatorPrincipal, parsed: SupplierRow) -> str:
        existing = self._find(parsed)
        fields = dict(parsed.values, name=parsed.name)
        if parsed.tax_id is not None:
            fields["tax_id"] = parsed.tax_id
        if "email" in fields:
            fields["email"] = fields["email"].lower()
        if existing is None:
            supplier = services.create_supplier(principal, **fields)
            if parsed.active is False:
                services.deactivate_supplier(principal, supplier.pk)
            return CREATED
        changes = {key: value for key, value in fields.items() if getattr(existing, key) != value}
        changed = bool(changes)
        if changes:
            services.update_supplier(principal, existing.pk, **changes)
        if parsed.active is not None and parsed.active != existing.is_active:
            action = services.reactivate_supplier if parsed.active else services.deactivate_supplier
            action(principal, existing.pk)
            changed = True
        return UPDATED if changed else UNCHANGED

    def _find(self, parsed: SupplierRow) -> Any:
        if parsed.tax_id is not None:
            found = selectors.supplier_by_tax_id(self.ctx, parsed.tax_id)
            if found is not None:
                return found
        named = selectors.supplier_by_name(self.ctx, parsed.name)
        if (
            named is not None
            and parsed.tax_id is not None
            and named.tax_id not in (None, parsed.tax_id)
        ):
            raise RowRefused(
                f"the name belongs to another supplier (tax id {named.tax_id})", "name"
            )
        return named
