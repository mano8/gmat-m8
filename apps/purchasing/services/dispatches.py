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

A buyer also sends an issued order from its page (M14.4, ``send_order``; ADR-0010 Q7 as
amended by Q82/Q83): under ``PO_DISPATCH`` ``manual`` or ``on``, the current revision is
queued to the supplier's **current** address, once at a time (refused while one of this
revision is queued or being sent), with a fresh ``Message-ID`` when the revision went out
before.

Each needs ``po.manage`` on the order, locks it ``FOR UPDATE`` (L4) and then its dispatch
rows (L4b), as cancel and amend do, and is audited on the order.
"""

from typing import cast
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db.models import QuerySet
from django.db.models.functions import Now
from django.utils.translation import gettext_lazy as _

from apps.access.principals import Principal
from apps.audit.recorder import record
from apps.core import events
from apps.core.idempotency import execute_once
from apps.core.locking import LockLevel, LockOrder, transactional
from apps.notifications.models import DISPATCH_KINDS, EmailOutbox, Kind
from apps.notifications.models import Status as Dispatch
from apps.purchasing.dispatch import (
    CONFIRMABLE,
    DECIDABLE,
    DispatchMode,
    lock_dispatches,
    mode,
    queue_revision,
    refusal,
    revision_of,
    source_ref,
)
from apps.purchasing.models import PurchaseOrder
from apps.purchasing.services.common import _authorize, _order, _record

IN_FLIGHT = (Dispatch.QUEUED, Dispatch.SENDING)


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


# --- a send from the order's page (M14.4) ---------------------------------------------------


def _revision_dispatches(order: PurchaseOrder) -> QuerySet[EmailOutbox]:
    return EmailOutbox.objects.filter(
        source_po=order, kind=Kind.PO_ORDER, source_ref=source_ref(order)
    )


def _sendable_to(order: PurchaseOrder) -> str:
    """The supplier's current address, once the order may be sent from its page (Q83)."""
    if mode() == DispatchMode.OFF:
        raise ValidationError(
            _("Orders are not emailed to suppliers on this installation: print it."),
            code="dispatch_off",
        )
    if order.status != PurchaseOrder.Status.ISSUED:
        raise ValidationError(_("Only an issued order is sent to the supplier."), code="not_issued")
    address = order.supplier.email.strip()
    if not address:
        raise ValidationError(
            _("The supplier has no email address: add it to the supplier, or print the order."),
            code="no_address",
        )
    if _revision_dispatches(order).filter(status__in=IN_FLIGHT).exists():
        raise ValidationError(
            _("This revision is already on its way to the supplier: wait for its status."),
            code="already_queued",
        )
    return address


@transactional
def send_order(
    principal: Principal, po_id: UUID, *, note: str = "", idempotency_key: str | None
) -> EmailOutbox:
    """Queue the issued order's current revision to its supplier's current address.

    §4.7: ``po.manage`` on the order, the idempotency record, the order ``FOR UPDATE`` (L4),
    its dispatch rows (L4b), then the checks under those locks; a refusal writes nothing. A
    double tap (the same key) returns the row the first one queued.
    """
    note = note.strip()
    locks = LockOrder()
    order = _order(po_id)
    _authorize(principal, order, locks)

    def execute() -> EmailOutbox:
        locks.lock(LockLevel.PO_HEADERS, PurchaseOrder, [order.pk])
        lock_dispatches(order.pk)(locks)
        locked = PurchaseOrder.objects.select_related("supplier").get(pk=order.pk)
        address = _sendable_to(locked)
        repeat = _revision_dispatches(locked).exists()
        message = queue_revision(locked, address, repeat=repeat)
        record(
            principal.audit_identity,
            "purchase_order.dispatch_sent_by_hand",
            obj=locked,
            branch_id=locked.branch_id,
            data={
                "number": locked.number,
                "dispatch_id": message.pk,
                "kind": message.kind,
                "revision": locked.revision,
                "address": address,
                "note": note,
                "repeat": repeat,
            },
        )
        events.publish("purchase_order.dispatch_queued", po_id=locked.pk, dispatch_id=message.pk)
        return message

    return execute_once(
        actor_id=principal.user_id,
        key=idempotency_key,
        operation="purchase_order.send",
        payload={"po_id": po_id, "note": note},
        execute=execute,
        replay=lambda _type, pk: EmailOutbox.objects.get(pk=pk),
    )
