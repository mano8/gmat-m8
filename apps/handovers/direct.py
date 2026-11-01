"""``DIRECT_RECEIPT`` handovers: created and voided only with their goods receipt (I8, C4).

Goods received at a worksite are handed over on the spot, so every attribution of a site
receipt gets exactly one handover line with the same request line and quantity, in one
handover per receipt. The receipt's services call these functions inside their
transaction, after authorizing the actor and locking the component (L2-L5) and the
receipt (L6); the handover comes next (L7) and its number last (L9).

A ``DIRECT_RECEIPT`` handover is never voided on its own: only ``void_for_receipt``, called
by the receipt's void, takes it to ``VOIDED``.
"""

from collections.abc import Sequence
from datetime import datetime

from django.conf import settings

from apps.access.principals import Principal
from apps.accounts.models import User
from apps.audit.recorder import record
from apps.core import events
from apps.core.locking import LockLevel, LockOrder
from apps.core.sequences import format_number, next_number
from apps.handovers.models import Handover, HandoverLine
from apps.handovers.transitions import transition
from apps.organizations.models import Branch
from apps.projects.models import Project
from apps.receiving.models import GoodsReceipt, ReceiptAttribution

Kind, Status = Handover.Kind, Handover.Status


def owner_code(branch: Branch) -> str:
    """``A-MAD``: branch codes are unique per company only."""
    return f"{branch.company.code}-{branch.code}"


def _assign_number(handover: Handover, branch: Branch, locks: LockOrder) -> None:
    """``ENT-{company}-{branch}-{yyyy}-{00001}`` per branch (L9, last)."""
    owner, year = owner_code(branch), handover.handed_at.year
    locks.enter(LockLevel.DOCUMENT_SEQUENCE)
    value = next_number(f"handover:{owner}:{year}")
    handover.number = format_number(settings.HANDOVER_NUMBER_PREFIX, owner, year, value)


def _record(principal: Principal, action: str, handover: Handover, **data: object) -> None:
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


def post_for_receipt(
    principal: Principal,
    actor: User,
    receipt: GoodsReceipt,
    project: Project,
    attributions: Sequence[ReceiptAttribution],
    locks: LockOrder,
) -> Handover | None:
    """The handover of a site receipt's attributions (none without one); audited.

    The actor received the goods on site and hands them over to the project.
    """
    if not attributions:
        return None
    handover = Handover(
        kind=Kind.DIRECT_RECEIPT,
        branch=receipt.branch,
        source_location=receipt.location,
        project=project,
        handed_by=actor,
        received_by=actor,
        handed_at=receipt.received_at,
        goods_receipt=receipt,
    )
    _assign_number(handover, receipt.branch, locks)
    handover.save()
    lines = HandoverLine.objects.bulk_create(
        HandoverLine(
            handover=handover,
            project=project,
            request_line_id=attribution.allocation.request_line_id,
            qty=attribution.qty,
            receipt_attribution=attribution,
        )
        for attribution in attributions
    )
    _record(
        principal,
        "handover.posted",
        handover,
        receipt_id=receipt.pk,
        receipt=receipt.number,
        project_id=project.pk,
        lines=[
            {"request_line_id": line.request_line_id, "qty": line.qty, "attribution_id": attr.pk}
            for line, attr in zip(lines, attributions, strict=True)
        ],
    )
    return handover


def void_for_receipt(
    principal: Principal,
    actor: User,
    receipt: GoodsReceipt,
    locks: LockOrder,
    *,
    reason: str,
    now: datetime,
) -> Handover | None:
    """Void the receipt's direct handover (L7) with the receipt; audited."""
    found = Handover.objects.filter(goods_receipt=receipt).values_list("pk", flat=True)
    locks.lock(LockLevel.HANDOVERS, Handover, found)
    handover = Handover.objects.filter(goods_receipt=receipt).first()
    if handover is None:
        return None
    transition(handover, Status.VOIDED, "with_receipt")
    handover.voided_at, handover.voided_by, handover.void_reason = now, actor, reason
    handover.save()
    _record(
        principal,
        "handover.voided",
        handover,
        receipt_id=receipt.pk,
        receipt=receipt.number,
        reason=reason,
    )
    return handover
