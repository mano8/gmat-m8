"""The handover state machine as one table (plan §4.3, §4.9).

A handover is posted once and can only be voided (with a reason); a voided handover is
final. A ``DIRECT_RECEIPT`` handover takes the edge only through its receipt's void
(``with_receipt``); a ``FROM_WAREHOUSE`` one through its own (M6.2).
"""

from types import MappingProxyType

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.handovers.models import Handover

Status = Handover.Status

# (from, to) → the operations that take that edge.
ALLOWED_TRANSITIONS: MappingProxyType[tuple[str, str], frozenset[str]] = MappingProxyType(
    {(Status.POSTED, Status.VOIDED): frozenset({"void", "with_receipt"})}
)


def transition(handover: Handover, target: str, operation: str) -> str:
    """Move ``handover`` to ``target`` (not saved); return the status it left."""
    current = handover.status
    operations = ALLOWED_TRANSITIONS.get((current, target))
    if operations is None or operation not in operations:
        raise ValidationError(
            _("This handover is %(status)s; it cannot be changed this way."),
            code="transition",
            params={"status": Status(current).label},
        )
    handover.status = target
    return current
