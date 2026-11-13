"""The receiving screens that only read (plan §11 M6.3): the receipts list and page, the
search for an order to receive, the receipt form and the site manager's expected deliveries.

Reads go through ``apps.receiving.selectors``. Posting and voiding are their own POST-only
URLs (``actions``). A site manager (``receipt.view`` / ``receipt.post`` ○ site) never reaches
the order's page: they receive from the expected deliveries of an assigned project, a
projection with the supplier name, the material, the date and what their project still
expects (§3.4), and the form they get shows nothing else of the order.
"""

from typing import Any
from uuid import UUID

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_safe

from apps.access.context import AccessContext
from apps.access.middleware import access_for
from apps.access.mixins import require_capability
from apps.attachments import selectors as attachment_selectors
from apps.core.listing import cursor_page, paginate, render_list
from apps.locations.models import Location
from apps.purchasing.models import PurchaseOrder
from apps.receiving import selectors
from apps.receiving.forms import (
    ExpectedFilterForm,
    OrderSearchForm,
    ProjectSearchForm,
    ReceiptFilterForm,
    ReceiptForm,
    RequiredReasonForm,
)
from apps.receiving.selectors import POST, VIEW
from apps.receiving.views.common import chosen_place, order_for_receipt, receive_url

# --- receipts -------------------------------

# The void dialog's fields, apart from any other form of the page.
VOID_FIELD_IDS = "id_void_%s"


@require_safe
@require_capability(VIEW)
def receipt_list(request: HttpRequest) -> HttpResponse:
    ctx = access_for(request)
    filters = ReceiptFilterForm(request.GET or None, ctx=ctx)
    data = filters.cleaned_data if filters.is_valid() else {}
    place = data.get("place")
    rows = selectors.receipts(
        ctx,
        status=data.get("status", ""),
        q=data.get("q", ""),
        location_id=place.pk if place else None,
        received_from=data.get("received_from"),
        received_to=data.get("received_to"),
    )
    listing = paginate(
        request,
        rows,
        default_per_page=selectors.DEFAULT_PER_PAGE,
        sorts=selectors.RECEIPT_SORTS,
        sort_labels=selectors.RECEIPT_SORT_LABELS,
    )
    context = {
        "filters": filters,
        "listing": listing,
        "can_post": selectors.can_post(ctx),
        "by_order": selectors.receives_by_order(ctx),
    }
    return render_list(request, "receiving/list.html", context)


@require_safe
@require_capability(VIEW)
def receipt_detail(request: HttpRequest, receipt_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    found = selectors.receipt(ctx, receipt_id)
    can_void = selectors.can_void(ctx, found)
    context: dict[str, Any] = {
        "receipt": found,
        "lines": selectors.receipt_lines(ctx, found),
        "handover": selectors.direct_handover(ctx, found),
        "project": selectors.receipt_project(ctx, found),
        "can_void": can_void,
        "void_form": RequiredReasonForm(auto_id=VOID_FIELD_IDS) if can_void else None,
        "attachments": attachment_selectors.panel(ctx, found),
    }
    return render(request, "receiving/detail.html", context)


# --- finding an order, receiving -------------------------------


@require_safe
@require_capability(POST)
def order_search(request: HttpRequest) -> HttpResponse:
    """Issued orders of the user's branches, found by number, supplier or project."""
    ctx = access_for(request)
    filters = OrderSearchForm(request.GET or None)
    data = filters.cleaned_data if filters.is_valid() else {}
    by_order = selectors.receives_by_order(ctx)
    rows = selectors.receivable_orders(ctx, q=data.get("q", ""))
    if not by_order:  # nothing to search: the projects lead to the forms (Q44)
        rows = rows.none()
    listing = paginate(
        request,
        rows,
        default_per_page=selectors.DEFAULT_PER_PAGE,
        sorts=selectors.ORDER_SORTS,
        sort_labels=selectors.ORDER_SORT_LABELS,
    )
    progress = selectors.receipt_progress(listing.rows)
    context = {
        "filters": filters,
        "listing": listing,
        "entries": [(found, progress.get(found.pk)) for found in listing.rows],
        "by_order": by_order,
    }
    return render_list(request, "receiving/orders.html", context)


def back_link(ctx: AccessContext, order: PurchaseOrder, place: Location | None) -> tuple[str, str]:
    """Where the receipt form's breadcrumb leads: to the order search when it lists the
    order, otherwise (who reaches the order only through a project: the site reach, or a
    branch that executes the project but does not buy for it) to the project's expected
    deliveries."""
    if selectors.in_order_search(ctx, order):
        return _("Receive goods"), reverse("receiving:orders")
    if place is not None and place.project_id is not None:
        return _("Expected deliveries"), reverse("receiving:expected", args=[place.project_id])
    return _("Expected on site"), reverse("receiving:projects")


def receive_page(
    request: HttpRequest,
    order: PurchaseOrder,
    places: list[Location],
    place: Location | None,
    form: ReceiptForm | None = None,
    status: int = 200,
) -> HttpResponse:
    """The receipt form of ``order`` for ``place`` (or why there is none)."""
    ctx = access_for(request)
    rows = selectors.receipt_rows(ctx, order, place) if place is not None else []
    if form is None and rows and place is not None:
        form = ReceiptForm(
            rows=rows, places=places, actor_id=ctx.user_id, initial={"location": str(place.pk)}
        )
    back_label, back_url = back_link(ctx, order, place)
    context = {
        "back_label": back_label,
        "back_url": back_url,
        "order": order,
        "places": places,
        "place": place,
        "rows": rows,
        "form": form,
        "whole_order": selectors.shows_whole_order(ctx, order),
        "entries": form.entries() if form is not None else [],
    }
    return render(request, "receiving/receive.html", context, status=status)


@require_safe
@require_capability(POST)
def receive(request: HttpRequest, po_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    order = order_for_receipt(request, po_id)
    places = selectors.receipt_locations(ctx, order)
    place = chosen_place(order, places, request.GET.get("location"))
    return receive_page(request, order, places, place)


# --- the site manager's expected deliveries -------------------------------


@require_safe
@require_capability(VIEW)
def project_list(request: HttpRequest) -> HttpResponse:
    """The projects with a worksite the user may receive at."""
    ctx = access_for(request)
    filters = ProjectSearchForm(request.GET or None)
    q = filters.cleaned_data["q"] if filters.is_valid() else ""
    listing = paginate(
        request,
        selectors.expected_projects(ctx, q=q),
        default_per_page=selectors.DEFAULT_PER_PAGE,
        sorts=selectors.PROJECT_SORTS,
        sort_labels=selectors.PROJECT_SORT_LABELS,
    )
    context = {"filters": filters, "listing": listing, "can_post": selectors.can_post(ctx)}
    return render_list(request, "receiving/projects.html", context)


@require_safe
@require_capability(VIEW)
def expected(request: HttpRequest, project_id: UUID) -> HttpResponse:
    """What is still coming to one project: §3.4's projection, grouped by order."""
    ctx = access_for(request)
    project = selectors.expected_project(ctx, project_id)
    filters = ExpectedFilterForm(request.GET or None)
    data = filters.cleaned_data if filters.is_valid() else {}
    narrow = selectors.ExpectedFilter(
        supplier=data.get("supplier", ""), expected_by=data.get("expected_by")
    )
    orders = selectors.ExpectedOrders(ctx, project, narrow)
    listing = cursor_page(
        request, orders.seek, orders.window, default_per_page=selectors.EXPECTED_PER_PAGE
    )
    place = selectors.worksite(ctx, project.pk)
    reads = selectors.worksite(ctx, project.pk, VIEW)
    context = {
        "project": project,
        "receipts_url": f"{reverse('receiving:list')}?place={reads.pk}" if reads else "",
        "filters": filters,
        "listing": listing,
        "orders": [
            (found, receive_url(found.po_id, place) if place is not None else "")
            for found in listing.rows
        ],
        "message": _("Nothing is expected here."),
        "filtered": bool(narrow.supplier or narrow.expected_by),
    }
    return render_list(request, "receiving/expected.html", context)
