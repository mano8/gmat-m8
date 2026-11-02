"""Shared helpers of the receiving screens."""

from uuid import UUID

from django.http import HttpRequest
from django.urls import reverse

from apps.access.middleware import access_for
from apps.locations.models import Location
from apps.purchasing.models import PurchaseOrder
from apps.receiving import selectors


def order_for_receipt(request: HttpRequest, po_id: UUID) -> PurchaseOrder:
    """The order if the user may receive its goods (404 otherwise; ``receipt.post`` is the
    selector's scope and the view's decorator)."""
    return selectors.receipt_order(access_for(request), po_id)


def chosen_place(
    order: PurchaseOrder, places: list[Location], wanted: str | None
) -> Location | None:
    """The place asked for if the user may receive there, else the order's ship-to if they
    may, else the first they may (``None`` when there is none)."""
    by_id = {str(place.pk): place for place in places}
    if wanted in by_id:
        return by_id[wanted]
    return by_id.get(str(order.ship_to_id)) or (places[0] if places else None)


def receive_url(po_id: UUID, place: Location | None = None) -> str:
    url = reverse("receiving:receive", args=[po_id])
    return f"{url}?location={place.pk}" if place is not None else url
