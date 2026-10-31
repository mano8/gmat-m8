"""The purchase-order side of supplier dispatches at cancellation (plan §5, F42, F53).

An order's ``PO_ORDER`` / ``PO_CANCELLATION`` outbox rows are locked after the order (L4 →
L4b) by every path, so a cancellation and a worker claim never deadlock (F55). In the
cancel transaction:

- every ``QUEUED`` ``PO_ORDER`` dispatch becomes ``CANCELLED``: nothing unsent goes out;
- if a ``PO_ORDER`` dispatch is ``SENDING``, ``SENT`` or ``AMBIGUOUS``, the supplier may
  have the order, so ``cancellation_notice_required`` is set on it.

Dispatches are queued from M5.3 (Q7), which also renders and queues the
``PO_CANCELLATION`` notice the obligation calls for (at once for a ``SENT`` or ``AMBIGUOUS``
dispatch, after the outcome for a ``SENDING`` one) and registers the worker's
``DispatchSource``.
"""

from collections.abc import Callable
from uuid import UUID

from django.utils import timezone

from apps.core.locking import LockLevel, LockOrder
from apps.notifications.models import DISPATCH_KINDS, EmailOutbox, Kind, Status
from apps.purchasing.models import PurchaseOrder

# A dispatch in these states may have reached the supplier.
IN_FLIGHT = frozenset({Status.SENDING, Status.SENT, Status.AMBIGUOUS})


def lock_dispatches(po_id: UUID) -> Callable[[LockOrder], list[UUID]]:
    """L4b: the order's dispatch rows, after its L4 lock (none can be added while held)."""

    def lock(locks: LockOrder) -> list[UUID]:
        rows = EmailOutbox.objects.filter(source_po_id=po_id, kind__in=DISPATCH_KINDS)
        return locks.lock(LockLevel.PO_OUTBOX, EmailOutbox, rows.values_list("pk", flat=True))

    return lock


def cancel_dispatches(order: PurchaseOrder) -> int:
    """Cancel the order's unsent dispatches; record a notice obligation for sent ones.

    Returns how many queued dispatches were cancelled. The order is saved by the caller.
    """
    orders = EmailOutbox.objects.filter(source_po=order, kind=Kind.PO_ORDER)
    cancelled = orders.filter(status=Status.QUEUED).update(
        status=Status.CANCELLED, updated_at=timezone.now()
    )
    if orders.filter(status__in=IN_FLIGHT).exists():
        order.cancellation_notice_required = True
    return cancelled
