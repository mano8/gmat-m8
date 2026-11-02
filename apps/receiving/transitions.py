"""The goods-receipt state machine as one table (plan §4.3, §4.9).

A receipt is posted once and can only be voided (with a reason); a voided receipt is final.
Every state change goes through ``transition()``, which refuses any pair outside
``ALLOWED_TRANSITIONS``.
"""

from types import MappingProxyType

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.receiving.models import GoodsReceipt

Status = GoodsReceipt.Status

# (from, to) → the operations that take that edge.
ALLOWED_TRANSITIONS: MappingProxyType[tuple[str, str], frozenset[str]] = MappingProxyType(
    {(Status.POSTED, Status.VOIDED): frozenset({"void"})}
)


def transition(receipt: GoodsReceipt, target: str, operation: str) -> str:
    """Move ``receipt`` to ``target`` (not saved); return the status it left."""
    current = receipt.status
    operations = ALLOWED_TRANSITIONS.get((current, target))
    if operations is None or operation not in operations:
        raise ValidationError(
            _("This goods receipt is %(status)s; it cannot be changed this way."),
            code="transition",
            params={"status": Status(current).label},
        )
    receipt.status = target
    return current
