"""A project's open requests follow its procurement branch (``apps.projects.procurement``).

A request in ``DRAFT``, ``SUBMITTED`` or ``APPROVED`` on another branch moves to the
project's new procurement branch, which then buys it and can ship it to the worksite
(one left on a branch by an earlier change moves too). One that is already committed
(allocated on an order, received or delivered) cannot move: the order that holds it
belongs to the old branch, so the change is refused and lists those requests. The
caller holds ``AccessState`` ``FOR UPDATE``, which every write waits for, so nothing is
allocated, received or delivered while this runs; the headers are locked at L2 all the
same.

Rejected, cancelled and closed requests keep the branch they were bought by. A closed
request reopened by hand (``services.reopen``) takes the project's procurement branch
then, under the same rule: only if nothing is committed; one that is keeps its branch,
whose orders and warehouses hold its goods (a handover is made by the request's branch).
"""

from uuid import UUID

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.access.principals import Principal
from apps.audit.recorder import record
from apps.core import events
from apps.core.locking import LockLevel
from apps.material_requests import ledger
from apps.material_requests.models import MaterialRequest, MaterialRequestLine
from apps.projects.models import Project
from apps.projects.procurement import ProcurementChange

Status = MaterialRequest.Status

MOVABLE = (Status.DRAFT, Status.SUBMITTED, Status.APPROVED)


def _committed(requests: list[MaterialRequest]) -> list[str]:
    """Numbers of the requests with anything allocated, received or delivered."""
    lines = MaterialRequestLine.objects.filter(request__in=requests)
    line_requests = dict(lines.values_list("pk", "request_id"))
    taken = {
        line_requests[line_id]
        for line_id, flow in ledger.flows(list(line_requests)).items()
        if flow != ledger.Flows()
    }
    return sorted(str(request) for request in requests if request.pk in taken)


def _move(principal: Principal, request: MaterialRequest, branch_id: UUID, cause: str) -> None:
    """Move one locked, uncommitted request to ``branch_id``, audited."""
    previous = request.procurement_branch_id
    request.procurement_branch_id = branch_id
    request.save(update_fields=["procurement_branch", "updated_at"])
    record(
        principal.audit_identity,
        "request.procurement_branch_changed",
        obj=request,
        branch_id=branch_id,
        data={"from": previous, "to": branch_id, "project_id": request.project_id, "cause": cause},
    )
    events.publish(
        "request.procurement_branch_changed", request_id=request.pk, procurement_branch_id=branch_id
    )


def follow_project(change: ProcurementChange) -> None:
    """Move the project's open requests to its new procurement branch, or refuse."""
    open_requests = MaterialRequest.objects.filter(
        project=change.project, status__in=MOVABLE
    ).exclude(procurement_branch_id=change.new_branch_id)
    ids = list(open_requests.values_list("pk", flat=True))
    if not ids:
        return
    change.locks.lock(LockLevel.REQUEST_HEADERS, MaterialRequest, ids)
    requests = list(open_requests.filter(pk__in=ids).order_by("pk"))
    committed = _committed(requests)
    if committed:
        raise ValidationError(
            {
                "procurement_branch": ValidationError(
                    _(
                        "These requests are already on purchase orders of the current "
                        "procurement branch: %(requests)s"
                    ),
                    code="committed_requests",
                    params={"requests": ", ".join(committed)},
                )
            }
        )
    for request in requests:
        _move(change.principal, request, change.new_branch_id, "project_changed")


def follow_on_reopen(principal: Principal, request: MaterialRequest) -> None:
    """A reopened request (locked by the caller) takes its project's procurement branch,
    unless something is committed. ``authorize_in_txn`` holds ``AccessState`` ``FOR
    SHARE``, so the project's branch cannot change meanwhile."""
    target = Project.objects.values_list("procurement_branch_id", flat=True).get(
        pk=request.project_id
    )
    if request.procurement_branch_id != target and not _committed([request]):
        _move(principal, request, target, "reopen")
