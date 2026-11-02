"""The purchasing screens that only read (plan §11 M5.2): the worklist, the builder's first
step, the order lists, the order page, the amendment form and the printable page.

Reads go through ``apps.purchasing.selectors``. The order page is one URL for every state:
it offers the builder's tools on a draft and the amendment, closing and cancelling tools on
an issued order, each only to who holds ``po.manage`` on it; a warehouse operator reads the
same page without prices (§8.2). Every action is its own POST-only URL (``actions``).
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

from django.conf import settings
from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_safe

from apps.access.middleware import access_for
from apps.access.mixins import require_capability
from apps.core.listing import cursor_page, paginate, render_list
from apps.purchasing import selectors
from apps.purchasing.forms import (
    AllocationEntry,
    AllocationQuantityForm,
    AmendForm,
    DispatchNoteForm,
    DraftHeaderForm,
    IssueForm,
    ItemsForm,
    LineEntry,
    LineForm,
    NewOrderForm,
    OrdersFilterForm,
    ReasonForm,
    RequiredReasonForm,
    WorklistFilterForm,
)
from apps.purchasing.models import PurchaseOrder, PurchaseOrderLine
from apps.purchasing.selectors import MANAGE, VIEW, AllocationRow, LineRow
from apps.purchasing.views.common import Status, _detail_url, _on_order


def _uuids(values: list[str]) -> list[UUID]:
    """The well-formed ids among ``values`` (anything else is ignored)."""
    found = []
    for value in values:
        try:
            found.append(UUID(value))
        except ValueError:
            continue
    return found


def _fresh(prefix: str, number: int | str) -> str:
    """An ``auto_id`` that stays unique when a page holds one form per line."""
    return f"id_%s_{prefix}{number}"


# --- the worklist ---------------------------------------------------------------------------


@require_safe
@require_capability(MANAGE)
def worklist(request: HttpRequest) -> HttpResponse:
    """What to buy by material, the drafts that reserve demand and the quantity on order for
    nobody; the lines are ticked and taken to the builder."""
    ctx = access_for(request)
    filters = WorklistFilterForm(request.GET or None, ctx=ctx)
    data = filters.cleaned_data if filters.is_valid() else {}
    project, supplier = data.get("project"), data.get("supplier")
    supplier_id = supplier.pk if supplier else None
    worklist = selectors.Worklist(
        ctx, project_id=project.pk if project else None, priority=data.get("priority", "")
    )
    nobody = selectors.Unallocated(ctx, supplier_id=supplier_id)
    drafts = paginate(
        request,
        selectors.drafts(ctx, supplier_id=supplier_id),
        default_per_page=selectors.SECTION_PER_PAGE,
        scope=selectors.DRAFTS_SCOPE,
    )
    unallocated = cursor_page(
        request,
        nobody.keys,
        nobody.window,
        default_per_page=selectors.SECTION_PER_PAGE,
        scope=selectors.UNALLOCATED_SCOPE,
    )
    context = {
        "filters": filters,
        "listing": cursor_page(
            request,
            worklist.keys,
            worklist.window,
            default_per_page=selectors.WORKLIST_PER_PAGE,
        ),
        "drafts": drafts,
        "draft_rows": [(draft, selectors.is_stale(draft)) for draft in drafts.rows],
        "unallocated": unallocated,
        "stale_days": selectors.STALE_DRAFT_DAYS,
    }
    return render_list(request, "purchasing/worklist.html", context)


# --- the builder's first step -----------------------------------------------------------------


def new_order_page(request: HttpRequest, form: NewOrderForm, status: int = 200) -> HttpResponse:
    return render(request, "purchasing/new.html", {"form": form}, status=status)


@require_safe
@require_capability(MANAGE)
def order_new(request: HttpRequest) -> HttpResponse:
    """Supplier, destination and the quantities of the lines ticked on the worklist."""
    ctx = access_for(request)
    ids = _uuids(request.GET.getlist("line"))
    rows = selectors.buyable(ctx, ids) if ids else []
    if ids and not rows:
        messages.error(request, _("Those lines have nothing left to purchase."))
        return redirect("purchasing:worklist")
    initial: dict[str, Any] = {}
    branches = {row.line.request.procurement_branch_id for row in rows}
    if len(branches) == 1:
        initial["branch"] = branches.pop()
    form = NewOrderForm(
        ctx=ctx,
        actor_id=ctx.user_id,
        rows=rows,
        picked={row.line.pk for row in rows},
        initial=initial,
    )
    return new_order_page(request, form)


# --- order lists -------------------------------------------------------------------------------


@require_safe
@require_capability(VIEW)
def order_list(request: HttpRequest) -> HttpResponse:
    """Orders to receive (issued) by default; any status and supplier by filter."""
    ctx = access_for(request)
    params = request.GET.copy()
    if params and "status" not in params:
        params["status"] = Status.ISSUED  # a sort or a page link keeps showing what is pending
    filters = OrdersFilterForm(params or None, ctx=ctx)
    data = filters.cleaned_data if filters.is_valid() else {}
    status = Status.ISSUED if "status" not in request.GET else data.get("status", "")
    supplier, project = data.get("supplier"), data.get("project")
    rows = selectors.orders(
        ctx,
        status=status,
        supplier_id=supplier.pk if supplier else None,
        project_id=project.pk if project else None,
        q=data.get("q", ""),
    )
    listing = paginate(
        request,
        rows,
        default_per_page=selectors.DEFAULT_PER_PAGE,
        sorts=selectors.ORDER_SORTS,
        sort_labels=selectors.ORDER_SORT_LABELS,
    )
    progress = selectors.progress(ctx, listing.rows)
    context = {
        "filters": filters,
        "listing": listing,
        "entries": [
            (
                found,
                progress.get(found.pk)
                if found.number and found.status != Status.CANCELLED
                else None,
            )
            for found in listing.rows
        ],
        "can_manage": selectors.can_manage(ctx),
    }
    return render_list(request, "purchasing/orders.html", context)


# --- the order page ----------------------------------------------------------------------------


@dataclass
class AllocationView:
    row: AllocationRow
    raise_form: AllocationQuantityForm | None = None
    lower_form: AllocationQuantityForm | None = None


@dataclass
class LineView:
    row: LineRow
    allocations: list[AllocationView]
    form: LineForm | None = None  # a draft's quantity and price
    close_form: RequiredReasonForm | None = None  # an issued line still receivable


def _allocation_views(
    line: PurchaseOrderLine, entries: list[AllocationRow], editable: bool
) -> list[AllocationView]:
    """A line's allocations; on a draft its manager raises or lowers each one."""
    views = []
    for number, entry in enumerate(entries, start=1):
        view = AllocationView(entry)
        if editable:
            tag = f"{line.line_no}_{number}"
            view.raise_form = AllocationQuantityForm(
                auto_id=_fresh("r", tag), initial={"qty": entry.allocation.qty}
            )
            view.lower_form = AllocationQuantityForm(auto_id=_fresh("l", tag))
        views.append(view)
    return views


def _line_view(
    row: LineRow, allocations: list[AllocationRow], status: str | None, prices: bool
) -> LineView:
    """A line with its tools for a manager: ``status`` is the order's (``None``: no tools)."""
    line = row.line
    view = LineView(row, _allocation_views(line, allocations, status == Status.DRAFT))
    if status == Status.DRAFT:
        view.form = LineForm(
            initial=LineForm.initial_for(line, prices=prices),
            prices=prices,
            auto_id=_fresh("line", line.line_no),
        )
    elif status == Status.ISSUED and line.closed_short_at is None:
        view.close_form = RequiredReasonForm(auto_id=_fresh("close", line.line_no))
    return view


def _line_views(
    ctx: Any, found: PurchaseOrder, managing: bool, prices: bool
) -> tuple[list[LineView], list[LineRow]]:
    rows = selectors.lines(ctx, found)
    by_line: dict[UUID, list[AllocationRow]] = {}
    for allocation in selectors.allocations(ctx, found):
        by_line.setdefault(allocation.allocation.po_line_id, []).append(allocation)
    status = found.status if managing else None
    views = [_line_view(row, by_line.get(row.line.pk, []), status, prices) for row in rows]
    return views, rows


@require_safe
@require_capability(VIEW)
def order_detail(request: HttpRequest, po_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    found = _on_order(request, po_id, VIEW)
    managing = selectors.can_manage(ctx, found)
    prices = selectors.shows_prices(ctx, found)
    lines, rows = _line_views(ctx, found, managing, prices)
    totals = [row.total for row in rows if row.total is not None]
    context: dict[str, Any] = {
        "order": found,
        "lines": lines,
        "managing": managing,
        "prices": prices,
        "order_total": sum(totals, Decimal(0)) if totals else None,
        "progress": selectors.progress_of(row.totals for row in rows)
        if found.status in {Status.ISSUED, Status.CLOSED}
        else None,
        "stale": selectors.is_stale(found),
        "stale_days": selectors.STALE_DRAFT_DAYS,
        "history": selectors.history(ctx, found),
        "printable": found.number is not None,
    }
    if managing:
        context |= _manager_tools(ctx, found)
    context |= _dispatch_panel(ctx, found)
    return render(request, "purchasing/detail.html", context)


def _manager_tools(ctx: Any, found: PurchaseOrder) -> dict[str, Any]:
    """The forms of the order page for who manages the order, by its state."""
    if found.status == Status.DRAFT:
        return {
            "header_form": DraftHeaderForm(
                initial=DraftHeaderForm.initial_for(found), ctx=ctx, order=found
            ),
            "items_form": ItemsForm(
                actor_id=ctx.user_id, rows=selectors.candidates(ctx, found), picked=set()
            ),
            "issue_form": IssueForm(actor_id=ctx.user_id),
            "discard_form": ReasonForm(auto_id=_fresh("discard", 0)),
        }
    if found.status == Status.ISSUED:
        return {"cancel_form": RequiredReasonForm(auto_id=_fresh("cancel", 0))}
    return {}


def _dispatch_panel(ctx: Any, found: PurchaseOrder) -> dict[str, Any]:
    """The order's supplier dispatches, each with its note form (M5.3)."""
    dispatches = selectors.dispatches(ctx, found)
    return {
        "dispatches": [
            (row, DispatchNoteForm(auto_id=_fresh("dispatch", index)))
            for index, row in enumerate(dispatches)
        ],
        "dispatch_on": settings.PO_DISPATCH,
        "awaiting_decision": selectors.awaiting_decision(dispatches),
        "notice_awaited": selectors.notice_awaited(found, dispatches),
    }


# --- the amendment form ---------------------------------------------------------------------------


@dataclass
class AmendBlock:
    entry: LineEntry
    allocations: list[tuple[AllocationRow, AllocationEntry]]


def amend_page(
    request: HttpRequest, found: PurchaseOrder, form: AmendForm, status: int = 200
) -> HttpResponse:
    ctx = access_for(request)
    by_line: dict[UUID, list[AllocationRow]] = {}
    for allocation in selectors.allocations(ctx, found):
        if allocation.allocation.pk in form.allocations:
            by_line.setdefault(allocation.allocation.po_line_id, []).append(allocation)
    blocks = [
        AmendBlock(
            entry,
            [
                (alloc, form.allocation_entry(alloc.allocation))
                for alloc in by_line.get(entry.line.pk, [])
            ],
        )
        for entry in form.line_entries()
    ]
    context = {
        "order": found,
        "form": form,
        "blocks": blocks,
        "prices": form.prices,
        "entries": form.entries(),
    }
    return render(request, "purchasing/amend.html", context, status=status)


def amend_form_for(request: HttpRequest, found: PurchaseOrder, data: Any = None) -> AmendForm:
    """The amendment form of an issued order: its receivable lines and live allocations."""
    ctx = access_for(request)
    open_lines = [
        row.line for row in selectors.lines(ctx, found) if row.line.closed_short_at is None
    ]
    line_ids = {line.pk for line in open_lines}
    live = [
        entry.allocation
        for entry in selectors.allocations(ctx, found)
        if entry.allocation.po_line_id in line_ids and entry.allocation.qty > 0
    ]
    return AmendForm(
        data,
        actor_id=ctx.user_id,
        order=found,
        lines=open_lines,
        allocations=live,
        prices=selectors.shows_prices(ctx, found),
        rows=selectors.candidates(ctx, found),
    )


@require_safe
@require_capability(MANAGE)
def order_amendment(request: HttpRequest, po_id: UUID) -> HttpResponse:
    found = _on_order(request, po_id)
    if found.status != Status.ISSUED:
        messages.error(request, _("Only an issued order is amended."))
        return redirect(_detail_url(found))
    return amend_page(request, found, amend_form_for(request, found))


# --- the printable page -------------------------------------------------------------------------


@require_safe
@require_capability(VIEW)
def order_print(request: HttpRequest, po_id: UUID) -> HttpResponse:
    """The order as issued, from its snapshots, laid out for paper (prices with ``price.view``)."""
    ctx = access_for(request)
    found = _on_order(request, po_id, VIEW)
    if found.number is None:
        messages.error(request, _("Issue the order to print it."))
        return redirect(_detail_url(found))
    prices = selectors.shows_prices(ctx, found)
    rows = selectors.lines(ctx, found)
    totals = [row.total for row in rows if row.total is not None]
    context = {
        "order": found,
        "lines": rows,
        "prices": prices,
        "order_total": sum(totals, Decimal(0)) if totals else None,
    }
    return render(request, "purchasing/print.html", context)
