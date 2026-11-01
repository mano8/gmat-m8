"""Shared helpers of the purchasing screens: scope-then-capability lookups, flash, redirect."""

from collections.abc import Callable
from urllib.parse import urlencode
from uuid import UUID

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.forms import Form
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.translation import gettext as _

from apps.access.middleware import access_for
from apps.access.mixins import require
from apps.core.forms import error_text, form_error_text
from apps.core.idempotency import IdempotencyError
from apps.core.locking import RetryExhausted
from apps.core.responses import conflict, idempotency_failure
from apps.notifications.models import EmailOutbox
from apps.purchasing import selectors
from apps.purchasing.models import PurchaseAllocation, PurchaseOrder, PurchaseOrderLine
from apps.purchasing.selectors import MANAGE, VIEW

Status = PurchaseOrder.Status


def _query(**params: str) -> str:
    """The filters that are set, for the pager's links."""
    kept = {key: value for key, value in params.items() if value}
    return f"&{urlencode(kept)}" if kept else ""


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


def _perform(
    request: HttpRequest,
    call: Callable[[], object],
    done: str,
    to: str,
    *,
    failed_to: str = "",
) -> HttpResponse:
    """Run a service call: flash its outcome and go to ``to`` (``failed_to`` if it failed)."""
    try:
        call()
    except ValidationError as exc:
        messages.error(request, error_text(exc))
        return redirect(failed_to or to)
    except IdempotencyError as exc:
        return idempotency_failure(request, exc)
    except RetryExhausted:
        return conflict(request)
    messages.success(request, done)
    return redirect(to)


def _invalid(request: HttpRequest, form: Form, to: str) -> HttpResponse:
    messages.error(request, form_error_text(form) or _("Check the form."))
    return redirect(to)
