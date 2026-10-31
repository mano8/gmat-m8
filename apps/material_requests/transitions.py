"""The request state machine as one table (plan §4.3).

Every state change goes through ``transition()``, which refuses any pair outside
``ALLOWED_TRANSITIONS``. The table says which pairs exist; each service adds the conditions
of its own operation (no decision yet for a withdrawal, nothing allocated or delivered for
a cancellation, plan §4.4 and §4.9).
"""

from types import MappingProxyType

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.material_requests.models import MaterialRequest

Status = MaterialRequest.Status

# (from, to) → the operations that take that edge (plan §4.3).
ALLOWED_TRANSITIONS: MappingProxyType[tuple[str, str], frozenset[str]] = MappingProxyType(
    {
        (Status.DRAFT, Status.SUBMITTED): frozenset({"submit"}),
        (Status.DRAFT, Status.CANCELLED): frozenset({"discard"}),
        (Status.SUBMITTED, Status.DRAFT): frozenset({"withdraw", "return_to_draft"}),
        (Status.SUBMITTED, Status.APPROVED): frozenset({"finalize"}),
        (Status.SUBMITTED, Status.REJECTED): frozenset({"finalize"}),
        (Status.SUBMITTED, Status.CANCELLED): frozenset({"cancel"}),
        (Status.APPROVED, Status.DRAFT): frozenset({"return_to_draft"}),
        (Status.APPROVED, Status.CANCELLED): frozenset({"cancel"}),
        (Status.APPROVED, Status.CLOSED): frozenset({"auto_close", "manual_close"}),
        (Status.CLOSED, Status.APPROVED): frozenset({"auto_reopen", "reopen"}),
    }
)

FINAL_STATES = frozenset({Status.REJECTED, Status.CANCELLED})


def is_allowed(current: str, target: str, operation: str | None = None) -> bool:
    """Whether ``current → target`` is in the table (taken by ``operation``, if given)."""
    operations = ALLOWED_TRANSITIONS.get((current, target))
    return operations is not None and (operation is None or operation in operations)


def transition(request: MaterialRequest, target: str, operation: str) -> str:
    """Move ``request`` to ``target`` (not saved); return the status it left.

    Raises a ``ValidationError`` (code ``transition``) for a pair or an operation outside
    the table.
    """
    current = request.status
    if not is_allowed(current, target, operation):
        raise ValidationError(
            _("This request is %(status)s; it cannot be changed this way."),
            code="transition",
            params={"status": Status(current).label},
        )
    request.status = target
    return current
