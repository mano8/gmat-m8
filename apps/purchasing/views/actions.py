"""The purchasing actions: each is its own POST-only URL that calls one service (plan §11
M5.2). A refusal is flashed and the user returns to the page; a form that cannot be sent
as it is (the builder's first step, the amendment) is shown again with its errors.

The order of the checks is S7's: the capability somewhere (403), the order in scope (404),
``po.manage`` on it (403). The services authorize and validate again in their transaction.
"""

from uuid import UUID

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.utils.translation import gettext as _
from django.utils.translation import ngettext

from apps.access.middleware import access_for
from apps.access.mixins import mutating
from apps.core.forms import add_service_errors, error_text
from apps.core.idempotency import IdempotencyError
from apps.core.locking import RetryExhausted
from apps.core.responses import conflict, idempotency_failure
from apps.purchasing import selectors, services
from apps.purchasing.forms import (
    AllocationQuantityForm,
    DraftHeaderForm,
    IssueForm,
    ItemsForm,
    LineForm,
    NewOrderForm,
    ReasonForm,
    RequiredReasonForm,
)
from apps.purchasing.selectors import MANAGE
from apps.purchasing.views.common import (
    Status,
    _detail_url,
    _invalid,
    _on_allocation,
    _on_line,
    _on_order,
    _perform,
)
from apps.purchasing.views.pages import _uuids, amend_form_for, amend_page, new_order_page

# --- a new order ----------------------------------------------------------------------------


@mutating(MANAGE)
def order_create(request: HttpRequest) -> HttpResponse:
    """Start the draft, then put the lines ticked on it (one idempotency key for each)."""
    ctx = access_for(request)
    posted = [key.removeprefix("pick_") for key in request.POST if key.startswith("pick_")]
    rows = selectors.to_purchase(ctx, line_ids=_uuids(posted)) if posted else []
    form = NewOrderForm(request.POST, ctx=ctx, rows=rows)
    ticked = {line_id for line_id in _uuids(posted) if request.POST.get(f"pick_{line_id}")}
    left_out = len(ticked - {row.line.pk for row in rows})
    if not form.is_valid():
        return new_order_page(request, form)
    try:
        order = services.create_po(
            ctx.principal,
            idempotency_key=form.cleaned_data["idempotency_key"],
            **form.header(),
        )
    except ValidationError as exc:
        add_service_errors(form, exc)
        return new_order_page(request, form)
    except IdempotencyError as exc:
        return idempotency_failure(request, exc)
    except RetryExhausted:
        return conflict(request)
    if left_out:
        messages.warning(
            request,
            ngettext(
                "%(count)s ticked line had nothing left to purchase and was left out.",
                "%(count)s ticked lines had nothing left to purchase and were left out.",
                left_out,
            )
            % {"count": left_out},
        )
    items = form.items()
    if not items:
        messages.success(request, _("Draft order created."))
        return redirect(_detail_url(order))
    return _perform(
        request,
        lambda: services.add_request_lines(
            ctx.principal,
            order.pk,
            items,
            idempotency_key=form.cleaned_data["items_key"] or None,
        ),
        _("Draft order created with %(count)s request lines.") % {"count": len(items)},
        _detail_url(order),
    )


# --- a draft ----------------------------------------------------------------------------------


@mutating(MANAGE)
def order_update(request: HttpRequest, po_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    found = _on_order(request, po_id)
    to = _detail_url(found)
    form = DraftHeaderForm(request.POST, ctx=ctx, order=found)
    if not form.is_valid():
        return _invalid(request, form, to)
    return _perform(
        request,
        lambda: services.update_order(ctx.principal, found.pk, **form.changes()),
        _("Draft saved."),
        to,
    )


@mutating(MANAGE)
def lines_add(request: HttpRequest, po_id: UUID) -> HttpResponse:
    """Request lines to buy on a draft: the ticked ones, with their quantities."""
    ctx = access_for(request)
    found = _on_order(request, po_id)
    to = _detail_url(found)
    form = ItemsForm(request.POST, rows=selectors.candidates(ctx, found))
    if not form.is_valid():
        return _invalid(request, form, to)
    return _perform(
        request,
        lambda: services.add_request_lines(
            ctx.principal,
            found.pk,
            form.items(),
            idempotency_key=form.cleaned_data["idempotency_key"],
        ),
        _("Request lines added."),
        to,
    )


@mutating(MANAGE)
def line_update(request: HttpRequest, line_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    line = _on_line(request, line_id)
    to = _detail_url(line)
    form = LineForm(request.POST, prices=selectors.shows_prices(ctx, line.po))
    if not form.is_valid():
        return _invalid(request, form, to)
    return _perform(
        request,
        lambda: services.update_line(ctx.principal, line.pk, **form.service_kwargs()),
        _("Line %(line)s saved.") % {"line": line.line_no},
        to,
    )


@mutating(MANAGE)
def line_remove(request: HttpRequest, line_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    line = _on_line(request, line_id)
    return _perform(
        request,
        lambda: services.remove_line(ctx.principal, line.pk),
        _("Line %(line)s removed.") % {"line": line.line_no},
        _detail_url(line),
    )


def _allocation_action(
    request: HttpRequest, allocation_id: UUID, *, raise_it: bool
) -> HttpResponse:
    ctx = access_for(request)
    allocation = _on_allocation(request, allocation_id)
    to = _detail_url(allocation)
    form = AllocationQuantityForm(request.POST)
    if not form.is_valid():
        return _invalid(request, form, to)
    qty = form.cleaned_data["qty"]
    if raise_it:
        return _perform(
            request,
            lambda: services.increase_allocation(ctx.principal, allocation.pk, qty=qty),
            _("Allocation raised."),
            to,
        )
    return _perform(
        request,
        lambda: services.unallocate(ctx.principal, allocation.pk, qty=qty),
        _("Allocation lowered."),
        to,
    )


@mutating(MANAGE)
def allocation_increase(request: HttpRequest, allocation_id: UUID) -> HttpResponse:
    return _allocation_action(request, allocation_id, raise_it=True)


@mutating(MANAGE)
def allocation_unallocate(request: HttpRequest, allocation_id: UUID) -> HttpResponse:
    return _allocation_action(request, allocation_id, raise_it=False)


@mutating(MANAGE)
def order_issue(request: HttpRequest, po_id: UUID) -> HttpResponse:
    """Number, snapshots and revalidation: the reservations become pending."""
    ctx = access_for(request)
    found = _on_order(request, po_id)
    to = _detail_url(found)
    form = IssueForm(request.POST)
    if not form.is_valid():
        return _invalid(request, form, to)
    try:
        issued = services.issue(
            ctx.principal, found.pk, idempotency_key=form.cleaned_data["idempotency_key"]
        )
    except ValidationError as exc:
        messages.error(request, error_text(exc))
    except IdempotencyError as exc:
        return idempotency_failure(request, exc)
    except RetryExhausted:
        return conflict(request)
    else:
        messages.success(request, _("Order %(number)s issued.") % {"number": issued.number})
    return redirect(to)


@mutating(MANAGE)
def order_cancel(request: HttpRequest, po_id: UUID) -> HttpResponse:
    """Discard a draft or cancel an issued order that has no receipt (a reason is required)."""
    ctx = access_for(request)
    found = _on_order(request, po_id)
    form = ReasonForm(request.POST)
    reason = form.cleaned_data["reason"] if form.is_valid() else ""
    issued = found.status == Status.ISSUED
    return _perform(
        request,
        lambda: services.cancel(ctx.principal, found.pk, reason=reason),
        _("Order cancelled.") if issued else _("Draft discarded."),
        _detail_url(found),
    )


# --- an issued order ------------------------------------------------------------------------------


@mutating(MANAGE)
def line_close_short(request: HttpRequest, line_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    line = _on_line(request, line_id)
    to = _detail_url(line)
    form = RequiredReasonForm(request.POST)
    if not form.is_valid():
        return _invalid(request, form, to)
    return _perform(
        request,
        lambda: services.close_line_short(
            ctx.principal, line.pk, reason=form.cleaned_data["reason"]
        ),
        _("Line %(line)s closed short.") % {"line": line.line_no},
        to,
    )


@mutating(MANAGE)
def order_amend(request: HttpRequest, po_id: UUID) -> HttpResponse:
    """An amendment: a reason, `revision + 1`; what did not change is not sent."""
    ctx = access_for(request)
    found = _on_order(request, po_id)
    form = amend_form_for(request, found, request.POST)
    if not form.is_valid():
        return amend_page(request, found, form)
    try:
        amended = services.amend(
            ctx.principal,
            found.pk,
            reason=form.cleaned_data["reason"],
            lines=form.line_amendments(),
            allocations=form.allocation_amendments(),
            add=form.items(),
            idempotency_key=form.cleaned_data["idempotency_key"],
            **form.header_changes(),
        )
    except ValidationError as exc:
        add_service_errors(form, exc)
        return amend_page(request, found, form)
    except IdempotencyError as exc:
        return idempotency_failure(request, exc)
    except RetryExhausted:
        return conflict(request)
    if amended.revision == found.revision:
        messages.info(
            request,
            _("Nothing changed: the order stays at revision %(n)s.") % {"n": found.revision},
        )
    else:
        messages.success(request, _("Order amended: revision %(n)s.") % {"n": amended.revision})
    return redirect(_detail_url(found))
