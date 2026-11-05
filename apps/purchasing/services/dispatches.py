"""A human's decision on a supplier dispatch the worker could not settle (plan §5, M5.3).

A dispatch that ended ``AMBIGUOUS`` (the supplier may have it) is never resent
automatically, one that ended ``FAILED`` has used its retries, and one a restore put in
``QUARANTINED`` may or may not have left before the backup (plan §9). A material manager
checks with the supplier, then:

- ``confirm_dispatch``: the supplier has it: ``AMBIGUOUS`` or ``QUARANTINED`` → ``SENT``;
- ``resend_dispatch``: send it again, a new attempt: ``AMBIGUOUS``, ``FAILED`` or
  ``QUARANTINED`` → ``QUEUED``, only while it may still be sent (a ``PO_ORDER`` of the current
  revision of an issued order, a ``PO_CANCELLATION`` of a cancelled one);
- ``discard_dispatch``: it must not go out: ``QUARANTINED`` → ``CANCELLED``.

Both need ``po.manage`` on the order, lock it ``FOR UPDATE`` (L4) and then its dispatch
rows (L4b), as cancel and amend do, and are audited on the order.
"""

from typing import cast
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db.models.functions import Now
from django.utils.translation import gettext_lazy as _

from apps.access.principals import Principal
from apps.core.locking import LockLevel, LockOrder, transactional
from apps.notifications.models import DISPATCH_KINDS, EmailOutbox
from apps.notifications.models import Status as Dispatch
from apps.purchasing.dispatch import (
    CONFIRMABLE,
    DECIDABLE,
    lock_dispatches,
    refusal,
    revision_of,
)
from apps.purchasing.models import PurchaseOrder
from apps.purchasing.services.common import _authorize, _order, _record


def _lock(
    principal: Principal, dispatch_id: UUID, locks: LockOrder
) -> tuple[PurchaseOrder, EmailOutbox]:
    """Authorize on the dispatch's order, then lock the order (L4) and the dispatch (L4b)."""
    found = EmailOutbox.objects.get(pk=dispatch_id, kind__in=DISPATCH_KINDS)
    order = _order(cast(UUID, found.source_po_id))  # a dispatch has its order
    _authorize(principal, order, locks)
    locks.lock(LockLevel.PO_HEADERS, PurchaseOrder, [order.pk])
    lock_dispatches(order.pk)(locks)
    return _order(order.pk), EmailOutbox.objects.get(pk=dispatch_id)


def _audit(
    principal: Principal, action: str, order: PurchaseOrder, message: EmailOutbox, note: str
) -> None:
    _record(
        principal,
        action,
        order,
        dispatch_id=message.pk,
        kind=message.kind,
        revision=revision_of(message),
        note=note.strip(),
    )


@transactional
def confirm_dispatch(principal: Principal, dispatch_id: UUID, *, note: str = "") -> EmailOutbox:
    """Record that the supplier received an ``AMBIGUOUS`` or ``QUARANTINED`` dispatch."""
    order, message = _lock(principal, dispatch_id, LockOrder())
    if message.status not in CONFIRMABLE:
        raise ValidationError(
            _("Only a dispatch whose delivery is uncertain can be confirmed."),
            code="not_ambiguous",
        )
    message.status = Dispatch.SENT
    message.sent_at = Now()
    message.save(update_fields=["status", "sent_at", "updated_at"])
    _audit(principal, "purchase_order.dispatch_confirmed", order, message, note)
    return message


@transactional
def resend_dispatch(principal: Principal, dispatch_id: UUID, *, note: str = "") -> EmailOutbox:
    """Queue a dispatch a human decided on again: the worker makes a new attempt."""
    order, message = _lock(principal, dispatch_id, LockOrder())
    if message.status not in DECIDABLE:
        raise ValidationError(
            _("Only a dispatch that failed or whose delivery is uncertain can be sent again."),
            code="not_decidable",
        )
    if refusal(order, message) is not None:
        raise ValidationError(
            _("The order has changed since: this dispatch is no longer sent."),
            code="not_eligible",
        )
    message.status = Dispatch.QUEUED
    message.not_before = Now()
    message.last_error = ""
    message.save(update_fields=["status", "not_before", "last_error", "updated_at"])
    _audit(principal, "purchase_order.dispatch_resent", order, message, note)
    return message


@transactional
def discard_dispatch(principal: Principal, dispatch_id: UUID, *, note: str = "") -> EmailOutbox:
    """A ``QUARANTINED`` dispatch never goes out (``manage outbox_quarantine``)."""
    order, message = _lock(principal, dispatch_id, LockOrder())
    if message.status != Dispatch.QUARANTINED:
        raise ValidationError(_("Only a quarantined dispatch can be discarded."), code="not_held")
    message.status = Dispatch.CANCELLED
    message.save(update_fields=["status", "updated_at"])
    _audit(principal, "purchase_order.dispatch_discarded", order, message, note)
    return message
