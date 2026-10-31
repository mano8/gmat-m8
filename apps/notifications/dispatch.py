"""The purchase-order side of supplier dispatches (plan §4.7 F55, §5).

``PO_ORDER`` and ``PO_CANCELLATION`` rows are tied to an order, so every path locks that
order first (L4) and the outbox row after it (L4b): the worker's claim and outcome take the
order ``FOR SHARE``, cancellation and amendment ``FOR UPDATE``. Only the purchasing app knows
the order model, so it registers a ``DispatchSource`` (M5.3) that:

- ``lock_order``: locks the order at L4 through the given ``LockOrder``;
- ``refusal``: returns ``None`` while the dispatch may still be sent, or the terminal status
  it gets instead (``CANCELLED``: the order is no longer issued; ``SUPERSEDED``: another
  revision is current). A ``PO_CANCELLATION`` is refused only if its order is not cancelled;
- ``after_outcome``: runs in the outcome transaction once a dispatch ends ``SENT`` or
  ``AMBIGUOUS``; for a ``PO_ORDER`` of an order cancelled meanwhile it queues the order's
  ``PO_CANCELLATION`` notice (the cancellation obligation, F53).

Until a source is registered, dispatch rows are never claimed.
"""

from collections.abc import Callable
from typing import Protocol
from uuid import UUID

from apps.core.locking import LockOrder
from apps.notifications.models import EmailOutbox, Status


class DispatchSource(Protocol):
    def lock_order(self, locks: LockOrder, po_id: UUID) -> None: ...

    def refusal(self, message: EmailOutbox) -> Status | None: ...

    def after_outcome(self, message: EmailOutbox, locks: LockOrder) -> None: ...


_source: list[DispatchSource] = []


def register_dispatch_source(source: DispatchSource) -> Callable[[], None]:
    """Install the purchasing app's source; return a function that removes it again."""
    if _source:
        raise ValueError("A dispatch source is already registered")
    _source.append(source)

    def unregister() -> None:
        if source in _source:
            _source.remove(source)

    return unregister


def dispatch_source() -> DispatchSource | None:
    return _source[0] if _source else None
