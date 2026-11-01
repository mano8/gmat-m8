"""The purchase-order state machine as one table (plan §4.3).

Every state change goes through ``transition()``, which refuses any pair outside
``ALLOWED_TRANSITIONS``. The table says which pairs exist; each service adds the conditions
of its own operation (no posted receipt for a cancellation; automatic close and reopen by
line capacity, §4.9). An amendment keeps the order ``ISSUED``: it is the table's one loop.
"""

from types import MappingProxyType

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.purchasing.models import PurchaseOrder

Status = PurchaseOrder.Status

# (from, to) → the operations that take that edge (plan §4.3).
ALLOWED_TRANSITIONS: MappingProxyType[tuple[str, str], frozenset[str]] = MappingProxyType(
    {
        (Status.DRAFT, Status.ISSUED): frozenset({"issue"}),
        (Status.DRAFT, Status.CANCELLED): frozenset({"discard"}),
        (Status.ISSUED, Status.ISSUED): frozenset({"amend"}),
        (Status.ISSUED, Status.CANCELLED): frozenset({"cancel"}),
        (Status.ISSUED, Status.CLOSED): frozenset({"auto_close"}),
        (Status.CLOSED, Status.ISSUED): frozenset({"auto_reopen"}),
    }
)

FINAL_STATES = frozenset({Status.CANCELLED})


def is_allowed(current: str, target: str, operation: str | None = None) -> bool:
    """Whether ``current → target`` is in the table (taken by ``operation``, if given)."""
    operations = ALLOWED_TRANSITIONS.get((current, target))
    return operations is not None and (operation is None or operation in operations)


def transition(order: PurchaseOrder, target: str, operation: str) -> str:
    """Move ``order`` to ``target`` (not saved); return the status it left.

    Raises a ``ValidationError`` (code ``transition``) for a pair or an operation outside
    the table.
    """
    current = order.status
    if not is_allowed(current, target, operation):
        raise ValidationError(
            _("This purchase order is %(status)s; it cannot be changed this way."),
            code="transition",
            params={"status": Status(current).label},
        )
    order.status = target
    return current
