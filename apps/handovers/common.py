"""What both kinds of handover share: the number and the audit record (plan §4.2, §4.10).

The services of both kinds call these inside their transaction, once the actor is
authorized and every lock below L9 is held.
"""

from django.conf import settings

from apps.access.principals import Principal
from apps.audit.recorder import record
from apps.core import events
from apps.core.locking import LockLevel, LockOrder
from apps.core.sequences import format_number, next_number
from apps.handovers.models import Handover
from apps.organizations.models import Branch


def owner_code(branch: Branch) -> str:
    """``A-MAD``: branch codes are unique per company only."""
    return f"{branch.company.code}-{branch.code}"


def assign_number(handover: Handover, branch: Branch, locks: LockOrder) -> None:
    """``ENT-{company}-{branch}-{yyyy}-{00001}`` per branch (L9, last)."""
    owner, year = owner_code(branch), handover.handed_at.year
    locks.enter(LockLevel.DOCUMENT_SEQUENCE)
    value = next_number(f"handover:{owner}:{year}")
    handover.number = format_number(settings.HANDOVER_NUMBER_PREFIX, owner, year, value)


def record_handover(principal: Principal, action: str, handover: Handover, **data: object) -> None:
    """Audit ``action`` on the handover and publish it (``handover.posted``, ``.voided``)."""
    record(
        principal.audit_identity,
        action,
        obj=handover,
        branch_id=handover.branch_id,
        data={"number": handover.number, "kind": handover.kind, **data},
    )
    events.publish(
        action, handover_id=handover.pk, project_id=handover.project_id, kind=handover.kind
    )
