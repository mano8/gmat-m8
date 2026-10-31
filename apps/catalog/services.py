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
from apps.catalog.models import Material, MaterialCategory
from apps.core import events
from apps.core.changes import apply_changes
from apps.core.db import constraint_errors
from apps.core.idempotency import execute_once
from apps.core.locking import LockOrder, transactional

MANAGE, VIEW = Capability.CATALOG_MANAGE, Capability.CATALOG_VIEW

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
    category = MaterialCategory.objects.get(pk=category_id)
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


@transactional
def create_material(
    principal: Principal,
    *,
    reference: str,
    name: str,
    unit_id: UUID,
    category_id: UUID | None = None,
    description: str = "",
    manufacturer: str = "",
    manufacturer_ref: str = "",
    ean: str | None = None,
    is_stock_item: bool = False,
    idempotency_key: str | None = None,
) -> Material:
    """Add a material to the catalog (active)."""
    _authorize(principal)
    values = _clean_text(
        {
            "reference": reference,
            "name": name,
            "description": description,
            "manufacturer": manufacturer,
            "manufacturer_ref": manufacturer_ref,
            "ean": ean,
        }
    )
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
    material = Material.objects.get(pk=material_id)
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
    material = Material.objects.get(pk=material_id)
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
