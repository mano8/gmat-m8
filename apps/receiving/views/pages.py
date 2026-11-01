"""The receiving screens that only read (plan §11 M6.3): the receipts list and page, the
search for an order to receive, the receipt form and the site manager's expected deliveries.

Reads go through ``apps.receiving.selectors``. Posting and voiding are their own POST-only
URLs (``actions``). A site manager (``receipt.view`` / ``receipt.post`` ○ site) never reaches
the order's page: they receive from the expected deliveries of an assigned project, a
projection with the supplier name, the material, the date and what their project still
expects (§3.4), and the form they get shows nothing else of the order.
"""

from itertools import groupby
from typing import Any
from uuid import UUID

from django.core.paginator import Paginator
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_safe

from apps.access.middleware import access_for
from apps.access.mixins import require_capability
from apps.core.http import is_htmx
from apps.locations.models import Location
from apps.purchasing.models import PurchaseOrder
from apps.receiving import selectors
from apps.receiving.forms import OrderSearchForm, ReceiptFilterForm, ReceiptForm, RequiredReasonForm
from apps.receiving.selectors import POST, VIEW
from apps.receiving.views.common import chosen_place, order_for_receipt, receive_url


def _extra_query(**params: str) -> str:
    kept = [f"&{key}={value}" for key, value in params.items() if value]
    return "".join(kept)


# --- receipts -------------------------------


@require_safe
@require_capability(VIEW)
def receipt_list(request: HttpRequest) -> HttpResponse:
    ctx = access_for(request)
    filters = ReceiptFilterForm(request.GET or None)
    data = filters.cleaned_data if filters.is_valid() else {}
    rows = selectors.receipts(ctx, status=data.get("status", ""), q=data.get("q", ""))
    page = Paginator(rows, selectors.PAGE_SIZE).get_page(request.GET.get("page"))
    template = "receiving/list.html#results" if is_htmx(request) else "receiving/list.html"
    context = {
        "filters": filters,
        "page_obj": page,
        "can_post": selectors.can_post(ctx),
        "extra_query": _extra_query(q=data.get("q", ""), status=data.get("status", "")),
    }
    return render(request, template, context)


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
        "can_void": can_void,
        "void_form": RequiredReasonForm(auto_id="id_void_%s") if can_void else None,
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
    rows = selectors.receivable_orders(ctx, q=data.get("q", ""))
    page = Paginator(rows, selectors.PAGE_SIZE).get_page(request.GET.get("page"))
    progress = selectors.receipt_progress(list(page.object_list))
    template = "receiving/orders.html#results" if is_htmx(request) else "receiving/orders.html"
    context = {
        "filters": filters,
        "page_obj": page,
        "entries": [(found, progress.get(found.pk)) for found in page.object_list],
        "extra_query": _extra_query(q=data.get("q", "")),
    }
    return render(request, template, context)


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
    context = {
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
    projects = selectors.expected_projects(access_for(request))
    page = Paginator(projects, selectors.PAGE_SIZE).get_page(request.GET.get("page"))
    return render(request, "receiving/projects.html", {"page_obj": page})


@require_safe
@require_capability(VIEW)
def expected(request: HttpRequest, project_id: UUID) -> HttpResponse:
    """What is still coming to one project: §3.4's projection, grouped by order."""
    ctx = access_for(request)
    project = selectors.expected_project(ctx, project_id)
    rows = selectors.expected_deliveries(ctx, project.pk)
    place = selectors.worksite(ctx, project.pk)
    groups = [
        (po_id, list(items), receive_url(po_id, place) if place is not None else "")
        for po_id, items in groupby(rows, key=lambda row: row.po_id)
    ]
    return render(
        request,
        "receiving/expected.html",
        {"project": project, "groups": groups, "message": _("Nothing is expected here.")},
    )
