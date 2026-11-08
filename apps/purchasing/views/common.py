"""Shared helpers of the purchasing screens: scope-then-capability lookups, flash, redirect."""

from uuid import UUID

from django.http import HttpRequest
from django.urls import reverse

from apps.access.middleware import access_for
from apps.access.mixins import require
from apps.core.responses import invalid, perform
from apps.notifications.models import EmailOutbox
from apps.purchasing import selectors
from apps.purchasing.models import PurchaseAllocation, PurchaseOrder, PurchaseOrderLine
from apps.purchasing.selectors import MANAGE, VIEW

Status = PurchaseOrder.Status


def _on_order(request: HttpRequest, po_id: UUID, capability: str = MANAGE) -> PurchaseOrder:
    """The order, if visible (404) and the user holds ``capability`` on it (403)."""
    found = selectors.order(access_for(request), po_id)
    require(request, capability, found, read=VIEW)
    return found


def _on_line(request: HttpRequest, line_id: UUID) -> PurchaseOrderLine:
    found = selectors.order_line(access_for(request), line_id)
    require(request, MANAGE, found.po, read=VIEW)
    return found


def _on_allocation(request: HttpRequest, allocation_id: UUID) -> PurchaseAllocation:
    found = selectors.allocation(access_for(request), allocation_id)
    require(request, MANAGE, found.po_line.po, read=VIEW)
    return found


def _on_dispatch(request: HttpRequest, dispatch_id: UUID) -> EmailOutbox:
    found = selectors.dispatch(access_for(request), dispatch_id)
    require(request, MANAGE, found.source_po, read=VIEW)
    return found


def _detail_url(
    found: PurchaseOrder | PurchaseOrderLine | PurchaseAllocation | EmailOutbox,
) -> str:
    if isinstance(found, EmailOutbox):
        po_id = found.source_po_id
    elif isinstance(found, PurchaseAllocation):
        po_id = found.po_line.po_id
    elif isinstance(found, PurchaseOrderLine):
        po_id = found.po_id
    else:
        po_id = found.pk
    return reverse("purchasing:detail", args=[po_id])


# One implementation of the POST answers for every screen (``apps.core.responses``, M8b.3):
# flashed and redirected, or the htmx dialog contract.
_perform = perform
_invalid = invalid
