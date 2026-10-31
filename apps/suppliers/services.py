"""Supplier services (plan §4.2, §3.6).

Same shape as the catalog's (``apps.catalog.services``): one transaction with
``lock_timeout`` through the retry wrapper, ``supplier.manage`` authorized in full inside it,
the idempotency key claimed by a create, validation, the write, ``audit.record`` and
``events.publish``. A change that alters nothing writes nothing. Suppliers are deactivated,
never deleted; purchase orders keep a snapshot of the supplier at issue (plan §4.2), so a
later edit never rewrites an issued document.

Natural keys (imports, M3.3): the ``tax_id`` when a supplier has one, else the ``name``
(unique ignoring case).
"""

from typing import Any
from uuid import UUID

from django.db.models import Model
from django.utils.translation import gettext_lazy as _

from apps.access.authorization import authorize_in_txn
from apps.access.principals import Principal
from apps.audit.recorder import record
from apps.core import events
from apps.core.changes import apply_changes
from apps.core.db import constraint_errors
from apps.core.idempotency import execute_once
from apps.core.locking import LockOrder, transactional
from apps.suppliers.models import Supplier
from apps.suppliers.selectors import MANAGE, VIEW

SUPPLIER_FIELDS = frozenset(
    {"name", "tax_id", "contact_name", "email", "phone", "address", "notes"}
)
SUPPLIER_CONSTRAINTS = {
    "suppliers_supplier_name": ("name", _("A supplier with this name exists.")),
    "suppliers_supplier_tax_id": ("tax_id", _("A supplier with this tax id exists.")),
}


def _authorize(principal: Principal, *objects: Model) -> None:
    authorize_in_txn(principal, MANAGE, *objects, locks=LockOrder(), read=VIEW)


def _clean(values: dict[str, Any]) -> dict[str, Any]:
    """Strip every text value; a blank tax id is none; emails are lower case."""
    cleaned = {k: v.strip() if isinstance(v, str) else v for k, v in values.items()}
    if "tax_id" in cleaned:
        cleaned["tax_id"] = cleaned["tax_id"] or None
    if "email" in cleaned:
        cleaned["email"] = cleaned["email"].lower()
    return cleaned


@transactional
def create_supplier(
    principal: Principal,
    *,
    name: str,
    tax_id: str | None = None,
    contact_name: str = "",
    email: str = "",
    phone: str = "",
    address: str = "",
    notes: str = "",
    idempotency_key: str | None = None,
) -> Supplier:
    """Add a supplier (active)."""
    _authorize(principal)
    values = _clean(
        {
            "name": name,
            "tax_id": tax_id,
            "contact_name": contact_name,
            "email": email,
            "phone": phone,
            "address": address,
            "notes": notes,
        }
    )

    def execute() -> Supplier:
        supplier = Supplier(**values)
        supplier.full_clean(validate_constraints=False)
        with constraint_errors(SUPPLIER_CONSTRAINTS):
            supplier.save()
        record(
            principal.audit_identity,
            "supplier.created",
            obj=supplier,
            data={"name": supplier.name, "tax_id": supplier.tax_id},
        )
        events.publish("supplier.created", supplier_id=supplier.pk)
        return supplier

    return execute_once(
        actor_id=principal.user_id,
        key=idempotency_key,
        operation="supplier.create",
        payload=values,
        execute=execute,
        replay=lambda _type, pk: Supplier.objects.get(pk=pk),
    )


@transactional
def update_supplier(principal: Principal, supplier_id: UUID, **changes: Any) -> Supplier:
    """Change a supplier's data (not whether it is active)."""
    supplier = Supplier.objects.get(pk=supplier_id)
    _authorize(principal, supplier)
    diff = apply_changes(supplier, _clean(changes), SUPPLIER_FIELDS)
    if not diff:
        return supplier
    supplier.full_clean(validate_constraints=False)
    with constraint_errors(SUPPLIER_CONSTRAINTS):
        supplier.save()
    # The audit trail keeps what changed; contact details are business data, not secrets.
    record(principal.audit_identity, "supplier.updated", obj=supplier, data=diff)
    events.publish("supplier.updated", supplier_id=supplier.pk, fields=sorted(diff))
    return supplier


def _set_active(principal: Principal, supplier_id: UUID, active: bool) -> Supplier:
    supplier = Supplier.objects.get(pk=supplier_id)
    _authorize(principal, supplier)
    if supplier.is_active == active:
        return supplier
    supplier.is_active = active
    supplier.save(update_fields=["is_active", "updated_at"])
    action = "reactivated" if active else "deactivated"
    record(principal.audit_identity, f"supplier.{action}", obj=supplier)
    events.publish(f"supplier.{action}", supplier_id=supplier.pk)
    return supplier


@transactional
def deactivate_supplier(principal: Principal, supplier_id: UUID) -> Supplier:
    """Stop offering a supplier for new orders; existing documents keep it."""
    return _set_active(principal, supplier_id, False)


@transactional
def reactivate_supplier(principal: Principal, supplier_id: UUID) -> Supplier:
    return _set_active(principal, supplier_id, True)
