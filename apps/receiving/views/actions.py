"""The receiving actions: each is its own POST-only URL that calls one service (plan §11
M6.3). The receipt form that cannot be posted as it is (a validation error of the form or of
the service) is shown again with its errors; a void flashes its outcome, or, sent from its
dialog with htmx, answers by the contract of ``apps.core.responses`` (M8b.3).

The order of the checks is S7's: the capability somewhere (403), the object in scope (404),
the capability on it (403). The services authorize and validate again in their transaction.
"""

from uuid import UUID

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.translation import gettext as _

from apps.access.exceptions import NotInScope
from apps.access.middleware import access_for
from apps.access.mixins import mutating, require
from apps.core.forms import add_service_errors
from apps.core.idempotency import IdempotencyError, recorded_result
from apps.core.locking import RetryExhausted
from apps.core.responses import Dialog, conflict, idempotency_failure, invalid, perform
from apps.receiving import selectors, services
from apps.receiving.forms import ReceiptForm, RequiredReasonForm
from apps.receiving.models import GoodsReceipt
from apps.receiving.selectors import POST, VIEW, VOID
from apps.receiving.views.common import order_for_receipt, receive_url
from apps.receiving.views.pages import VOID_FIELD_IDS, receive_page

# The void dialog's form (M8b.3): shown again alone, with its errors, to an htmx POST.
VOID_FORM = "receiving/detail.html#void_form"


def _post_receipt(request: HttpRequest, po_id: UUID, form: ReceiptForm) -> HttpResponse | None:
    """Post a valid form: the redirect to the receipt, or ``None`` when the service refused
    it (its errors are then on the form)."""
    ctx = access_for(request)
    data = form.cleaned_data
    try:
        receipt = services.post_receipt(
            ctx.principal,
            po_id,
            location_id=data["location"],
            lines=form.line_inputs(),
            delivery_note_ref=data["delivery_note_ref"],
            notes=data["notes"],
            idempotency_key=data["idempotency_key"],
        )
    except ValidationError as exc:
        add_service_errors(form, exc)
        return None
    except IdempotencyError as exc:
        return idempotency_failure(request, exc)
    except RetryExhausted:
        return conflict(request)
    messages.success(request, _("Receipt %(number)s posted.") % {"number": receipt.number})
    return redirect("receiving:detail", receipt_id=receipt.pk)


def _already_posted(request: HttpRequest) -> HttpResponse | None:
    """A form that is no longer valid because its key already posted a receipt (a double tap
    that used the order up): go to that receipt instead of showing errors."""
    ctx = access_for(request)
    key = request.POST.get("idempotency_key", "")
    if not key:
        return None
    try:
        done = recorded_result(key, actor_id=ctx.principal.user_id, operation=ReceiptForm.operation)
    except IdempotencyError as exc:
        return idempotency_failure(request, exc)
    if done is None or done[0] != GoodsReceipt._meta.label_lower:
        return None
    try:
        receipt = selectors.receipt(ctx, done[1])
    except NotInScope:
        return None
    messages.info(request, _("Receipt %(number)s was already posted.") % {"number": receipt.number})
    return redirect("receiving:detail", receipt_id=receipt.pk)


@mutating(POST)
def receipt_post(request: HttpRequest, po_id: UUID) -> HttpResponse:
    """Post what arrived. A double tap carries the same key and returns the same receipt."""
    ctx = access_for(request)
    order = order_for_receipt(request, po_id)
    places = selectors.receipt_locations(ctx, order)
    wanted = request.POST.get("location")
    place = next((found for found in places if str(found.pk) == wanted), None)
    if place is None:
        answer = _already_posted(request)  # the first tap may have closed the place
        if answer is not None:
            return answer
        messages.error(request, _("Choose where the goods arrived."))
        return redirect(receive_url(order.pk))
    rows = selectors.receipt_rows(ctx, order, place)
    form = ReceiptForm(request.POST, rows=rows, places=places, actor_id=ctx.user_id)
    answer = _post_receipt(request, order.pk, form) if form.is_valid() else _already_posted(request)
    return answer if answer is not None else receive_page(request, order, places, place, form)


@mutating(VOID)
def receipt_void(request: HttpRequest, receipt_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    found = selectors.receipt(ctx, receipt_id)
    require(request, VOID, found, read=VIEW)
    to = reverse("receiving:detail", args=[found.pk])
    form = RequiredReasonForm(request.POST, auto_id=VOID_FIELD_IDS)
    dialog = Dialog(form, VOID_FORM, lambda bound: {"receipt": found, "void_form": bound})
    if not form.is_valid():
        return invalid(request, form, to, dialog=dialog)
    return perform(
        request,
        lambda: services.void_receipt(ctx.principal, found.pk, reason=form.cleaned_data["reason"]),
        _("Receipt %(number)s voided.") % {"number": found.number},
        to,
        dialog=dialog,
    )
