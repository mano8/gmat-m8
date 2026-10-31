"""Reads for suppliers (plan §4.2, M3.2): only ``supplier.view`` holders see a supplier.

Suppliers are installation-wide master data, so the capability alone decides: group
administrators, material managers and warehouse operators read every supplier; site
managers, technicians and partners read none (a site manager meets the supplier's display
name only inside the expected-deliveries projection of an assigned project, plan §3.4).
"""

from uuid import UUID

from django.db.models import Q, QuerySet

from apps.access.context import AccessContext
from apps.access.exceptions import NotInScope
from apps.access.roles import Capability
from apps.suppliers.models import Supplier

PAGE_SIZE = 25

VIEW, MANAGE = Capability.SUPPLIER_VIEW, Capability.SUPPLIER_MANAGE

STATE_ACTIVE, STATE_INACTIVE, STATE_ALL = "active", "inactive", "all"


def suppliers(ctx: AccessContext, *, q: str = "", state: str = STATE_ACTIVE) -> QuerySet[Supplier]:
    """The suppliers ``ctx`` may view, filtered by text and state, by name."""
    rows = Supplier.objects.visible_to(ctx, VIEW)
    if state == STATE_ACTIVE:
        rows = rows.filter(is_active=True)
    elif state == STATE_INACTIVE:
        rows = rows.filter(is_active=False)
    if q:
        rows = rows.filter(
            Q(name__icontains=q)
            | Q(tax_id__icontains=q)
            | Q(contact_name__icontains=q)
            | Q(email__icontains=q)
        )
    return rows.order_by("name")


def supplier(ctx: AccessContext, supplier_id: UUID) -> Supplier:
    """A supplier ``ctx`` may view, inactive ones included (orders keep pointing at them)."""
    found = Supplier.objects.visible_to(ctx, VIEW).filter(pk=supplier_id).first()
    if found is None:
        raise NotInScope(VIEW)
    return found


def manageable(ctx: AccessContext, of: Supplier | None = None) -> bool:
    """Whether ``ctx`` may add or change suppliers (the services check again)."""
    if of is None:
        return ctx.has(MANAGE)
    return ctx.can(MANAGE, of)


def supplier_by_tax_id(ctx: AccessContext, tax_id: str) -> Supplier | None:
    return Supplier.objects.visible_to(ctx, VIEW).filter(tax_id=tax_id).first()


def supplier_by_name(ctx: AccessContext, name: str) -> Supplier | None:
    """The supplier with this name, ignoring case, if viewable (the name is unique)."""
    return Supplier.objects.visible_to(ctx, VIEW).filter(name__iexact=name).first()
