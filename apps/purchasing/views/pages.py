"""The purchasing screens that only read (plan §11 M5.2): the worklist, the builder's first
step, the order lists, the order page, the amendment form and the printable page.

Reads go through ``apps.purchasing.selectors``. The order page is one URL for every state:
it offers the builder's tools on a draft and the amendment, closing and cancelling tools on
an issued order, each only to who holds ``po.manage`` on it; a warehouse operator reads the
same page without prices (§8.2). Every action is its own POST-only URL (``actions``).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

from django.conf import settings
from django.contrib import messages
from django.forms import Form
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_safe

from apps.access.middleware import access_for
from apps.access.mixins import require_capability
from apps.core.dialogs import ActionDialog
from apps.core.listing import cursor_page, paginate, render_list
from apps.purchasing import selectors
from apps.purchasing.forms import (
    AllocationEntry,
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
from apps.purchasing.models import PurchaseOrder
from apps.purchasing.selectors import MANAGE, VIEW, AllocationRow, LineRow
from apps.purchasing.views.common import Status, _detail_url, _on_order
from apps.purchasing.views.order_dialogs import (
    allocation_form,
    cancel_dialog,
    confirm_dialog,
    discard_dialog,
    fresh,
    header_dialog,
    line_close_dialog,
    line_edit_dialog,
    line_remove_dialog,
    lower_dialog,
    raise_dialog,
    resend_dialog,
)


def _uuids(values: list[str]) -> list[UUID]:
    """The well-formed ids among ``values`` (anything else is ignored)."""
    found = []
    for value in values:
        try:
            found.append(UUID(value))
        except ValueError:
            continue
    return found


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
        nobody.seek,
        nobody.window,
        default_per_page=selectors.SECTION_PER_PAGE,
        scope=selectors.UNALLOCATED_SCOPE,
        first_stretch=selectors.UNALLOCATED_STRETCH,
    )
    context = {
        "filters": filters,
        "listing": cursor_page(
            request,
            worklist.seek,
            worklist.window,
            default_per_page=selectors.WORKLIST_PER_PAGE,
            scope=selectors.WORKLIST_SCOPE,
            first_stretch=selectors.WORKLIST_STRETCH,
        ),
        "drafts": drafts,
        "draft_rows": [(draft, selectors.is_stale(draft)) for draft in drafts.rows],
        "unallocated": unallocated,
        "stale_days": selectors.STALE_DRAFT_DAYS,
    }
    return render_list(request, "purchasing/worklist.html", context)


# --- the builder's first step -----------------------------------------------------------------


def new_order_page(request: HttpRequest, form: NewOrderForm, status: int = 200) -> HttpResponse:
    context = {
        "form": form,
        "no_suppliers": not form.fields["supplier"].queryset.exists(),  # type: ignore[attr-defined]
    }
    return render(request, "purchasing/new.html", context, status=status)


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


def _with_progress(
    orders: Sequence[PurchaseOrder], progress: Mapping[UUID, Any]
) -> list[tuple[PurchaseOrder, Any]]:
    """Each order with its receipt progress; a draft or a cancelled order has none."""
    return [
        (
            found,
            progress.get(found.pk) if found.number and found.status != Status.CANCELLED else None,
        )
        for found in orders
    ]


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
    status = data.get("status", "") if params else Status.ISSUED
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
        "entries": _with_progress(listing.rows, progress),
        "can_manage": selectors.can_manage(ctx),
    }
    return render_list(request, "purchasing/orders.html", context)


# --- the order page ----------------------------------------------------------------------------


@dataclass
class AllocationView:
    row: AllocationRow
    raise_it: ActionDialog | None = None  # the dialogs that raise or lower a draft allocation
    lower: ActionDialog | None = None


@dataclass
class LineView:
    row: LineRow
    allocations: list[AllocationView]
    edit: ActionDialog | None = None  # a draft's quantity and price
    remove: ActionDialog | None = None  # a draft line
    close: ActionDialog | None = None  # an issued line still receivable


def _allocation_views(entries: list[AllocationRow], editable: bool) -> list[AllocationView]:
    """A line's allocations; on a draft its manager raises or lowers each one."""
    views = []
    for entry in entries:
        view = AllocationView(entry)
        if editable:
            allocation = entry.allocation
            raising = allocation_form(allocation, raising=True, initial={"qty": allocation.qty})
            view.raise_it = raise_dialog(allocation, raising)
            view.lower = lower_dialog(allocation, allocation_form(allocation, raising=False))
        views.append(view)
    return views


def _line_view(
    row: LineRow, allocations: list[AllocationRow], status: str | None, prices: bool
) -> LineView:
    """A line with its tools for a manager: ``status`` is the order's (``None``: no tools)."""
    line = row.line
    view = LineView(row, _allocation_views(allocations, status == Status.DRAFT))
    if status == Status.DRAFT:
        edit = LineForm(
            initial=LineForm.initial_for(line, prices=prices),
            prices=prices,
            auto_id=fresh("line", line.line_no),
        )
        view.edit = line_edit_dialog(line, edit)
        view.remove = line_remove_dialog(line, Form(auto_id=fresh("remove", line.line_no)))
    elif status == Status.ISSUED and line.closed_short_at is None:
        close = RequiredReasonForm(auto_id=fresh("close", line.line_no))
        view.close = line_close_dialog(line, close)
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
        context["menu_items"] = [
            context[key].menu_item()
            for key in ("header_dialog", "discard_dialog", "cancel_dialog")
            if key in context
        ]
    context |= _dispatch_panel(ctx, found)
    return render(request, "purchasing/detail.html", context)


def _manager_tools(ctx: Any, found: PurchaseOrder) -> dict[str, Any]:
    """The forms and dialogs of the order page for who manages the order, by its state."""
    if found.status == Status.DRAFT:
        rows = selectors.candidates(ctx, found)
        header = DraftHeaderForm(initial=DraftHeaderForm.initial_for(found), ctx=ctx, order=found)
        return {
            "header_dialog": header_dialog(found, header),
            "items_form": ItemsForm(actor_id=ctx.user_id, rows=rows, picked=set()),
            "no_candidates": None if rows else selectors.no_candidates(ctx, found),
            "issue_form": IssueForm(actor_id=ctx.user_id),
            "discard_dialog": discard_dialog(found, ReasonForm(auto_id=fresh("discard", 0))),
        }
    if found.status == Status.ISSUED:
        form = RequiredReasonForm(auto_id=fresh("cancel", 0))
        return {"cancel_dialog": cancel_dialog(found, form)}
    return {}


def _dispatch_panel(ctx: Any, found: PurchaseOrder) -> dict[str, Any]:
    """The order's supplier dispatches, each with its dialogs for the decision (M5.3)."""
    dispatches = selectors.dispatches(ctx, found)
    return {
        "dispatches": [_dispatch_row(row) for row in dispatches],
        "dispatch_on": settings.PO_DISPATCH,
        "awaiting_decision": selectors.awaiting_decision(dispatches),
        "notice_awaited": selectors.notice_awaited(found, dispatches),
    }


def _dispatch_row(row: Any) -> tuple[Any, ActionDialog, ActionDialog]:
    """A dispatch with the dialogs that send it again and confirm it (each a note)."""
    dispatch = row.dispatch
    again = resend_dialog(dispatch, DispatchNoteForm(auto_id=fresh("resend", dispatch.pk)))
    confirm = confirm_dialog(dispatch, DispatchNoteForm(auto_id=fresh("confirm", dispatch.pk)))
    return row, again, confirm


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
    entries = form.entries()
    context = {
        "order": found,
        "form": form,
        "blocks": blocks,
        "prices": form.prices,
        "entries": entries,
        "no_candidates": None if entries else selectors.no_candidates(ctx, found),
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
