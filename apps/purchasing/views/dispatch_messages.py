"""What an action did with the supplier's emails, said after it (M14.4, Q86).

Issuing, amending, sending and cancelling an order flash what was queued and to which
address, what was withdrawn and what is owed, under ``PO_DISPATCH`` ``manual`` and ``on``
(``off`` keeps the action's own message alone). Each flash is worked out from the order as it
is after the call (``selectors.dispatch_notice``; a send, from the email the service
returned, read again on a replay), never from the form, so a double tap or a replay says the
same. A message is *queued*, never *sent*, until the worker says so. Only
``po.manage`` holders reach these views, so naming the supplier's address is theirs to read.
"""

from django.contrib import messages
from django.http import HttpRequest
from django.utils.translation import gettext as _
from django.utils.translation import ngettext

from apps.access.middleware import access_for
from apps.notifications.models import EmailOutbox
from apps.notifications.selectors import IN_FLIGHT
from apps.purchasing import selectors
from apps.purchasing.dispatch import DispatchMode
from apps.purchasing.models import PurchaseOrder
from apps.purchasing.selectors import DispatchNotice


def _notice(request: HttpRequest, order: PurchaseOrder) -> DispatchNotice:
    ctx = access_for(request)
    return selectors.dispatch_notice(ctx, selectors.order(ctx, order.pk))


def _queued_text(message: EmailOutbox) -> str:
    """One email of the order: queued (or already on its way) to its address."""
    if message.status in IN_FLIGHT:
        return _("Queued to be emailed to %(address)s.") % {"address": message.recipient_address}
    return _("The email to %(address)s is %(status)s: see Sent to the supplier.") % {
        "address": message.recipient_address,
        "status": message.get_status_display().lower(),
    }


def _withdrawn(request: HttpRequest, count: int) -> None:
    if count:
        messages.info(
            request,
            ngettext(
                "An email to the supplier, not sent yet, was withdrawn.",
                "%(count)s emails to the supplier, not sent yet, were withdrawn.",
                count,
            )
            % {"count": count},
        )


def _revision(request: HttpRequest, notice: DispatchNotice) -> None:
    """The order's current revision under ``manual`` or ``on`` after an issue or amendment."""
    if notice.latest is not None:
        messages.success(request, _queued_text(notice.latest))
    elif notice.mode == DispatchMode.MANUAL:
        messages.info(
            request, _("Not emailed to the supplier: use Send to the supplier to email it.")
        )
    else:
        messages.warning(
            request,
            _(
                "Not emailed to the supplier: it had no email address when the order was "
                "issued. Print the order and send it."
            ),
        )


def after_issue(request: HttpRequest, order: PurchaseOrder) -> None:
    notice = _notice(request, order)
    if notice.mode != DispatchMode.OFF:
        _revision(request, notice)


def after_amend(request: HttpRequest, order: PurchaseOrder) -> None:
    notice = _notice(request, order)
    if notice.mode == DispatchMode.OFF:
        return
    _withdrawn(request, notice.withdrawn)
    _revision(request, notice)


def sent_text(order: PurchaseOrder, message: EmailOutbox) -> str:
    """The flash of a send from the order's page: the email it queued, as it is now (a
    replay reads it again, so a double tap after the worker sent it says so)."""
    return f"{order.number}: {_queued_text(message)}"


def after_cancel(request: HttpRequest, order: PurchaseOrder) -> None:
    notice = _notice(request, order)
    if notice.mode == DispatchMode.OFF or order.number is None:
        return
    _withdrawn(request, notice.withdrawn)
    if notice.cancellation is not None:
        messages.info(
            request,
            _("A cancellation notice is queued to be emailed to %(address)s.")
            % {"address": notice.cancellation.recipient_address},
        )
    elif notice.notice_awaited:
        messages.info(
            request,
            _(
                "The order is being emailed to the supplier right now: if it arrives, a "
                "cancellation notice follows."
            ),
        )
