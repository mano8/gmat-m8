"""Catalog services: categories and materials (plan §4.2, §3.6).

The catalog is master data shared by the whole installation, so a change takes no
scope-defining lock: each service runs in one transaction with ``lock_timeout`` through the
shared retry wrapper, authorizes ``catalog.manage`` (L0 ``FOR SHARE`` rows, in full, every
time), claims its idempotency key when it creates, validates, writes and ends with
``audit.record`` and ``events.publish``. Material rows are never locked by a service: C9
(a referenced material keeps its unit) is held by the composite foreign key, and an update
takes the row lock of its own ``UPDATE``.

- A material's ``reference`` is its natural key, fixed once created (imports and documents
  point at it); to rename one, deactivate the material and create another.
- A change that alters nothing writes nothing and audits nothing, which keeps a repeated
  import a no-op.
- Materials are deactivated, never deleted.
- Units of measure (Q72, ADR-0030) are installation-wide master data too, managed by
  ``org.manage``: ``create_unit`` and ``update_unit`` refuse a change of ``code`` or a lower
  ``decimals`` while anything uses the unit, and never delete. ``update_unit`` locks the unit's
  row ``FOR UPDATE``, so two changes of one unit (or of two codes to the same one) serialize.
  A document that is written in the same instant is not held up (the foreign keys are checked at
  commit, and a line reads its unit without a lock), so the check is a reading, as the dialogs'
  previews are: ADR-0030 records that window.
"""

from typing import Any
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db.models import Model
from django.utils.translation import gettext_lazy as _

from apps.access.authorization import authorize_in_txn
from apps.access.principals import Principal
from apps.access.roles import Capability
from apps.audit.recorder import record
from apps.catalog.models import Material, MaterialCategory, UnitOfMeasure
from apps.catalog.selectors import unit_in_use
from apps.core import events
from apps.core.changes import apply_changes
from apps.core.db import constraint_errors
from apps.core.idempotency import execute_once
from apps.core.locking import LockOrder, transactional

MANAGE, VIEW = Capability.CATALOG_MANAGE, Capability.CATALOG_VIEW
ORG = Capability.ORG_MANAGE

CATEGORY_FIELDS = frozenset({"name", "parent_id"})
MATERIAL_FIELDS = frozenset(
    {
        "name",
        "description",
        "category_id",
        "unit_id",
        "manufacturer",
        "manufacturer_ref",
        "ean",
        "is_stock_item",
    }
)

UNIT_FIELDS = frozenset({"code", "name", "decimals", "is_active"})

CATEGORY_CONSTRAINTS = {
    "catalog_category_root_name": ("name", _("A category with this name already exists.")),
    "catalog_category_child_name": (
        "name",
        _("This category already has a subcategory with this name."),
    ),
    "catalog_category_not_own_parent": (
        "parent",
        _("A category cannot be its own parent."),
    ),
    "catalog_category_two_levels": (
        "parent",
        _("Categories have two levels at most."),
    ),
}
MATERIAL_CONSTRAINTS = {
    "catalog_material_reference": ("reference", _("A material with this reference exists.")),
}
UNIT_CONSTRAINTS = {
    "catalog_unitofmeasure_code_key": ("code", _("A unit with this code exists.")),
}


# ``full_clean(validate_constraints=False)`` below: the database settles these constraints in
# one place, under concurrency too, and ``constraint_errors`` words the refusal per field.


def _authorize(principal: Principal, *objects: Model) -> None:
    authorize_in_txn(principal, MANAGE, *objects, locks=LockOrder(), read=VIEW)


def _clean_text(values: dict[str, Any]) -> dict[str, Any]:
    """Strip every text value; a blank barcode is none."""
    cleaned = {k: v.strip() if isinstance(v, str) else v for k, v in values.items()}
    if "ean" in cleaned:
        cleaned["ean"] = cleaned["ean"] or None
    return cleaned


# --- categories ---------------------------------------------------------------------


def _check_parent(category: MaterialCategory) -> None:
    """Friendly errors for the two-level rule; the trigger holds it against races."""
    if category.parent_id is None:
        return
    parent = MaterialCategory.objects.filter(pk=category.parent_id).first()
    if parent is not None and parent.parent_id is not None:
        raise ValidationError(
            {"parent": ValidationError(_("Categories have two levels at most."), code="depth")}
        )
    if (
        category._state.adding is False
        and MaterialCategory.objects.filter(parent_id=category.pk).exists()
    ):
        raise ValidationError(
            {
                "parent": ValidationError(
                    _("A category with subcategories cannot be placed under another."),
                    code="depth",
                )
            }
        )


@transactional
def create_category(
    principal: Principal,
    *,
    name: str,
    parent_id: UUID | None = None,
    idempotency_key: str | None = None,
) -> MaterialCategory:
    _authorize(principal)
    values = _clean_text({"name": name})
    payload = {**values, "parent_id": parent_id}

    def execute() -> MaterialCategory:
        category = MaterialCategory(name=values["name"], parent_id=parent_id)
        category.full_clean(validate_constraints=False)
        _check_parent(category)
        with constraint_errors(CATEGORY_CONSTRAINTS):
            category.save()
        record(
            principal.audit_identity,
            "category.created",
            obj=category,
            data={"name": category.name, "parent_id": parent_id},
        )
        events.publish("category.created", category_id=category.pk)
        return category

    return execute_once(
        actor_id=principal.user_id,
        key=idempotency_key,
        operation="category.create",
        payload=payload,
        execute=execute,
        replay=lambda _type, pk: MaterialCategory.objects.get(pk=pk),
    )


@transactional
def update_category(principal: Principal, category_id: UUID, **changes: Any) -> MaterialCategory:
    category = MaterialCategory.objects.select_for_update().get(pk=category_id)
    _authorize(principal, category)
    diff = apply_changes(category, _clean_text(changes), CATEGORY_FIELDS)
    if not diff:
        return category
    category.full_clean(validate_constraints=False)
    if "parent_id" in diff:
        _check_parent(category)
    with constraint_errors(CATEGORY_CONSTRAINTS):
        category.save()
    record(principal.audit_identity, "category.updated", obj=category, data=diff)
    events.publish("category.updated", category_id=category.pk, fields=sorted(diff))
    return category


# --- materials ----------------------------------------------------------------------


MATERIAL_DEFAULTS: dict[str, Any] = {
    "category_id": None,
    "description": "",
    "manufacturer": "",
    "manufacturer_ref": "",
    "ean": None,
    "is_stock_item": False,
}


@transactional
def create_material(
    principal: Principal,
    *,
    reference: str,
    name: str,
    unit_id: UUID,
    idempotency_key: str | None = None,
    **optional: Any,
) -> Material:
    """Add a material to the catalog (active).

    ``optional`` takes any of ``MATERIAL_DEFAULTS`` (``category_id``, ``description``,
    ``manufacturer``, ``manufacturer_ref``, ``ean``, ``is_stock_item``); another key is a
    ``TypeError``.
    """
    unknown = sorted(set(optional) - MATERIAL_DEFAULTS.keys())
    if unknown:
        raise TypeError(f"create_material() got unexpected arguments: {unknown}")
    given = MATERIAL_DEFAULTS | optional
    _authorize(principal)
    is_stock_item, category_id = given.pop("is_stock_item"), given.pop("category_id")
    values = _clean_text({"reference": reference, "name": name, **given})
    payload = {**values, "unit_id": unit_id, "category_id": category_id, "stock": is_stock_item}

    def execute() -> Material:
        material = Material(
            unit_id=unit_id, category_id=category_id, is_stock_item=is_stock_item, **values
        )
        material.full_clean(validate_constraints=False)
        with constraint_errors(MATERIAL_CONSTRAINTS):
            material.save()
        record(
            principal.audit_identity,
            "material.created",
            obj=material,
            data={
                "reference": material.reference,
                "name": material.name,
                "unit_id": unit_id,
                "category_id": category_id,
                "is_stock_item": is_stock_item,
            },
        )
        events.publish("material.created", material_id=material.pk)
        return material

    return execute_once(
        actor_id=principal.user_id,
        key=idempotency_key,
        operation="material.create",
        payload=payload,
        execute=execute,
        replay=lambda _type, pk: Material.objects.get(pk=pk),
    )


@transactional
def update_material(principal: Principal, material_id: UUID, **changes: Any) -> Material:
    """Change a material's data (not its ``reference``, nor whether it is active).

    A material that documents reference keeps its unit: the composite foreign key refuses
    the change (C9), which is reported on the ``unit`` field.
    """
    material = Material.objects.select_for_update().get(pk=material_id)
    _authorize(principal, material)
    diff = apply_changes(material, _clean_text(changes), MATERIAL_FIELDS)
    if not diff:
        return material
    material.full_clean(validate_constraints=False)
    with constraint_errors(
        MATERIAL_CONSTRAINTS,
        referenced=("unit", _("A material that documents use keeps its unit.")),
    ):
        material.save()
    record(principal.audit_identity, "material.updated", obj=material, data=diff)
    events.publish("material.updated", material_id=material.pk, fields=sorted(diff))
    return material


def _set_active(principal: Principal, material_id: UUID, active: bool) -> Material:
    material = Material.objects.select_for_update().get(pk=material_id)
    _authorize(principal, material)
    if material.is_active == active:
        return material
    material.is_active = active
    material.save(update_fields=["is_active", "updated_at"])
    action = "reactivated" if active else "deactivated"
    record(principal.audit_identity, f"material.{action}", obj=material)
    events.publish(f"material.{action}", material_id=material.pk)
    return material


@transactional
def deactivate_material(principal: Principal, material_id: UUID) -> Material:
    """Hide a material from searches and new documents; existing documents keep it."""
    return _set_active(principal, material_id, False)


@transactional
def reactivate_material(principal: Principal, material_id: UUID) -> Material:
    return _set_active(principal, material_id, True)


# --- units of measure (Q72, ADR-0030) -----------------------------------------------


def _authorize_units(principal: Principal) -> None:
    """Units are installation-wide, yet a group administrator's to manage: ``org.manage``."""
    authorize_in_txn(principal, ORG, locks=LockOrder())


def _clean_unit(values: dict[str, Any]) -> dict[str, Any]:
    """A code is kept upper-case (imports compare it so; the key is then unique ignoring case);
    text is stripped."""
    cleaned = _clean_text(values)
    if isinstance(cleaned.get("code"), str):
        cleaned["code"] = cleaned["code"].upper()
    return cleaned


def _check_unit_in_use(unit: UnitOfMeasure, diff: dict[str, dict[str, Any]]) -> None:
    """Q72: ``code`` is fixed, and ``decimals`` never lowered, once anything uses the unit
    (a material, a request line, an order line): its quantities were entered under it."""
    lowered = "decimals" in diff and diff["decimals"]["to"] < diff["decimals"]["from"]
    if not ("code" in diff or lowered) or not unit_in_use(unit):
        return
    errors = {}
    if "code" in diff:
        errors["code"] = ValidationError(
            _("A unit in use keeps its code. Add a new unit and deactivate this one."),
            code="in_use",
        )
    if lowered:
        errors["decimals"] = ValidationError(
            _("Quantities in a unit in use keep their decimals; they can only be raised."),
            code="in_use",
        )
    raise ValidationError(errors)


@transactional
def create_unit(
    principal: Principal,
    *,
    code: str,
    name: str,
    decimals: int = 0,
    idempotency_key: str | None = None,
) -> UnitOfMeasure:
    """Add a unit of measure (active)."""
    _authorize_units(principal)
    values = _clean_unit({"code": code, "name": name})
    payload = {**values, "decimals": decimals}

    def execute() -> UnitOfMeasure:
        unit = UnitOfMeasure(decimals=decimals, **values)
        unit.full_clean(validate_constraints=False)
        with constraint_errors(UNIT_CONSTRAINTS):
            unit.save()
        record(
            principal.audit_identity,
            "unit.created",
            obj=unit,
            data={"code": unit.code, "name": unit.name, "decimals": decimals},
        )
        events.publish("unit.created", unit_id=unit.pk)
        return unit

    return execute_once(
        actor_id=principal.user_id,
        key=idempotency_key,
        operation="unit.create",
        payload=payload,
        execute=execute,
        replay=lambda _type, pk: UnitOfMeasure.objects.get(pk=pk),
    )


@transactional
def update_unit(principal: Principal, unit_id: UUID, **changes: Any) -> UnitOfMeasure:
    """Change a unit's ``name``, ``decimals``, ``code`` or whether it is active.

    ``code`` is fixed and ``decimals`` only raised once anything uses the unit (Q72). An
    inactive unit leaves the choices for new materials and imports; whatever has it keeps it.
    """
    _authorize_units(principal)
    unit = UnitOfMeasure.objects.select_for_update().get(pk=unit_id)
    diff = apply_changes(unit, _clean_unit(changes), UNIT_FIELDS)
    if not diff:
        return unit
    unit.full_clean(validate_constraints=False)
    _check_unit_in_use(unit, diff)
    with constraint_errors(UNIT_CONSTRAINTS):
        unit.save()
    record(principal.audit_identity, "unit.updated", obj=unit, data=diff)
    events.publish("unit.updated", unit_id=unit.pk, fields=sorted(diff))
    return unit
