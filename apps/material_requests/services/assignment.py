"""Reviewer assignment after the submit (M7b.2, Q31, ADR-0014 §4).

The requester picks a reviewer at submit (``requester.submit``); afterwards any reviewer
in scope (``request.review``) may send the request to another one, with a note. The
assignee must qualify (``reviewers.covers``); the assignment never restricts who decides.
"""

from uuid import UUID

from apps.access.principals import Principal
from apps.core import events
from apps.core.locking import LockOrder, transactional
from apps.material_requests.models import MaterialRequest
from apps.material_requests.services.common import (
    REVIEW,
    Status,
    _authorize,
    _lock_header,
    _lock_people,
    _reason,
    _record,
    _require_state,
    _set_reviewer,
)


@transactional
def assign_reviewer(
    principal: Principal, request_id: UUID, *, reviewer_id: UUID, note: str
) -> MaterialRequest:
    """Assign a submitted or approved request to ``reviewer_id`` (reassign: another one).

    The note is required and audited with the previous and the new assignee. Assigning
    the current assignee again changes nothing. The new assignee is emailed (the
    ``request.reviewer_assigned`` notice), unless they assigned it to themselves.
    """
    note = _reason(note, "note")
    locks = LockOrder()
    request = MaterialRequest.objects.get(pk=request_id)
    _lock_people(locks, principal, reviewer_id)
    _authorize(principal, REVIEW, request, locks)
    locked = _lock_header(locks, request)
    _require_state(locked, Status.SUBMITTED, Status.APPROVED)
    diff = _set_reviewer(locked, reviewer_id, principal.user_id)
    if not diff:
        return locked
    locked.save()
    _record(principal, "request.reviewer_assigned", locked, changes=diff, note=note)
    events.publish(
        "request.reviewer_assigned",
        request_id=locked.pk,
        reviewer_id=reviewer_id,
        assigned_by=principal.user_id,
        note=note,
    )
    return locked
