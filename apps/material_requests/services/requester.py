"""Requester side: drafts, lines, submit, withdraw, cancel (plan §4.3, §4.4)."""

from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db.models import Max
from django.utils.translation import gettext_lazy as _

from apps.access.principals import Principal
from apps.core import events
from apps.core.changes import apply_changes
from apps.core.db import db_now
from apps.core.idempotency import execute_once
from apps.core.locking import LockOrder, LockSetChanged, transactional
from apps.material_requests.models import MaterialRequest, MaterialRequestLine
from apps.material_requests.services.common import (
    CANCEL_ANY,
    CREATE,
    EDITABLE_FIELDS,
    LINE_FIELDS,
    SUBMIT_FIELDS,
    WITHDRAW,
    Status,
    _assign_number,
    _authorize,
    _authorize_project,
    _decided,
    _line_data,
    _line_request,
    _line_values,
    _lock_header,
    _lock_line,
    _lock_lines,
    _project,
    _record,
    _require_draft,
    _require_nothing_allocated_or_delivered,
    _require_open_project,
    _reset_decisions,
    _save_line,
)
from apps.material_requests.transitions import transition

# --- header ---------------------------------------------------------------------------


@transactional
def create_draft(
    principal: Principal,
    *,
    project_id: UUID,
    priority: str = MaterialRequest.Priority.NORMAL,
    needed_by: date | None = None,
    notes: str = "",
    idempotency_key: str | None = None,
) -> MaterialRequest:
    """Start a draft request for a project in the actor's ``request.create`` scope."""
    locks = LockOrder()
    _authorize_project(principal, _project(project_id), locks)
    payload = {
        "project_id": project_id,
        "priority": priority,
        "needed_by": needed_by,
        "notes": notes,
    }

    def execute() -> MaterialRequest:
        project = _project(project_id)  # under the L0 lock: its scope cannot change now
        _require_open_project(project)
        request = MaterialRequest(
            project=project,
            procurement_branch_id=project.procurement_branch_id,
            requested_by_id=principal.user_id,
            priority=priority,
            needed_by=needed_by,
            notes=notes.strip(),
        )
        request.full_clean(validate_constraints=False)
        request.save()
        _record(
            principal,
            "request.created",
            request,
            project_id=project_id,
            priority=priority,
            needed_by=needed_by,
        )
        events.publish("request.created", request_id=request.pk, project_id=project_id)
        return request

    return execute_once(
        actor_id=principal.user_id,
        key=idempotency_key,
        operation="request.create",
        payload=payload,
        execute=execute,
        replay=lambda _type, pk: MaterialRequest.objects.get(pk=pk),
    )


@transactional
def update_request(principal: Principal, request_id: UUID, **changes: Any) -> MaterialRequest:
    """Change an own request's header: everything in ``DRAFT``, notes once ``APPROVED``.

    A draft moved to another project takes that project's procurement branch; its lines
    follow through the cascading ``(request_id, project_id)`` key.
    """
    locks = LockOrder()
    request = MaterialRequest.objects.get(pk=request_id)
    _authorize(principal, CREATE, request, locks)
    if "project_id" in changes and changes["project_id"] != request.project_id:
        _authorize_project(principal, _project(changes["project_id"]), locks)
    locked = _lock_header(locks, request)
    allowed = EDITABLE_FIELDS.get(locked.status)
    if allowed is None or not set(changes) <= allowed:
        raise ValidationError(
            _("These details cannot be changed while the request is %(status)s."),
            code="not_editable",
            params={"status": Status(locked.status).label},
        )
    if isinstance(changes.get("notes"), str):
        changes["notes"] = changes["notes"].strip()
    diff = apply_changes(locked, changes, allowed)
    if not diff:
        return locked
    if "project_id" in diff:
        project = _project(locked.project_id)
        _require_open_project(project)
        locked.procurement_branch_id = project.procurement_branch_id
    locked.full_clean(validate_constraints=False)
    locked.save()
    _record(principal, "request.updated", locked, changes=diff)
    events.publish("request.updated", request_id=locked.pk, fields=sorted(diff))
    return locked


# --- lines ----------------------------------------------------------------------------


@transactional
def add_line(
    principal: Principal,
    request_id: UUID,
    *,
    requested_qty: Decimal,
    material_id: UUID | None = None,
    free_text: str = "",
    unit_id: UUID | None = None,
    idempotency_key: str | None = None,
) -> MaterialRequestLine:
    """Add a catalog line (``material_id``) or a non-catalog line (``free_text``, ``unit_id``)."""
    locks = LockOrder()
    request = MaterialRequest.objects.get(pk=request_id)
    _authorize(principal, CREATE, request, locks)
    payload = {
        "request_id": request_id,
        "material_id": material_id,
        "free_text": free_text,
        "unit_id": unit_id,
        "requested_qty": requested_qty,
    }

    def execute() -> MaterialRequestLine:
        locked = _lock_header(locks, request)
        _require_draft(locked)
        values = _line_values(
            material_id=material_id,
            free_text=free_text,
            unit_id=unit_id,
            requested_qty=requested_qty,
        )
        last = locked.lines.aggregate(last=Max("line_no"))["last"] or 0
        line = MaterialRequestLine(
            request=locked, project_id=locked.project_id, line_no=last + 1, **values
        )
        _save_line(line)
        _record(principal, "request_line.added", locked, line_id=line.pk, **_line_data(line))
        events.publish("request_line.added", request_id=locked.pk, line_id=line.pk)
        return line

    return execute_once(
        actor_id=principal.user_id,
        key=idempotency_key,
        operation="request_line.add",
        payload=payload,
        execute=execute,
        replay=lambda _type, pk: MaterialRequestLine.objects.get(pk=pk),
    )


@transactional
def update_line(principal: Principal, line_id: UUID, **changes: Any) -> MaterialRequestLine:
    """Change a draft line's material, description, unit or quantity.

    A catalog line's unit follows its material; ``unit_id`` is only for non-catalog lines
    (or must equal the material's).
    """
    unknown = sorted(set(changes) - LINE_FIELDS)
    if unknown:
        raise ValueError(f"Request line fields that cannot be changed here: {unknown}")
    locks = LockOrder()
    request = _line_request(line_id)
    _authorize(principal, CREATE, request, locks)
    locked = _lock_header(locks, request)
    _require_draft(locked)
    line = _lock_line(locks, line_id)
    merged = {
        "material_id": line.material_id,
        "free_text": line.free_text,
        "unit_id": line.unit_id,
        "requested_qty": line.requested_qty,
    } | changes
    if merged["material_id"] is not None and "unit_id" not in changes:
        merged["unit_id"] = None  # the material's own unit
    values = _line_values(
        **merged, new_material=merged["material_id"] not in {None, line.material_id}
    )
    diff = apply_changes(line, values, LINE_FIELDS)
    if not diff:
        return line
    _save_line(line)
    _record(
        principal,
        "request_line.updated",
        locked,
        line_id=line.pk,
        line_no=line.line_no,
        changes=diff,
    )
    events.publish("request_line.updated", request_id=locked.pk, line_id=line.pk)
    return line


@transactional
def remove_line(principal: Principal, line_id: UUID) -> MaterialRequest:
    """Delete a draft line (nothing refers to a draft's lines); the audit keeps it."""
    locks = LockOrder()
    request = _line_request(line_id)
    _authorize(principal, CREATE, request, locks)
    locked = _lock_header(locks, request)
    _require_draft(locked)
    line = _lock_line(locks, line_id)
    data = _line_data(line)
    line.delete()
    _record(principal, "request_line.removed", locked, line_id=line_id, **data)
    events.publish("request_line.removed", request_id=locked.pk, line_id=line_id)
    return locked


# --- transitions ----------------------------------------------------------------------


@transactional
def submit(
    principal: Principal,
    request_id: UUID,
    *,
    idempotency_key: str | None = None,
    **header: Any,
) -> MaterialRequest:
    """Send a draft for review: take the procurement-branch snapshot, assign the number.

    The number is taken at the first submit and kept if the request comes back to draft.
    With an idempotency key a repeated submit (a double tap) returns the request again.
    ``header`` (``priority``, ``needed_by``, ``notes``) is the review step of the mobile
    flow: it is saved in the same transaction, so a double tap cannot leave a submitted
    request with half of the details.
    """
    unknown = sorted(set(header) - SUBMIT_FIELDS)
    if unknown:
        raise ValueError(f"Request fields that cannot be set at submit: {unknown}")
    locks = LockOrder()
    request = MaterialRequest.objects.get(pk=request_id)
    _authorize(principal, CREATE, request, locks)
    project = _project(request.project_id)
    _require_open_project(project)
    _authorize_project(principal, project, locks)

    def execute() -> MaterialRequest:
        locked = _lock_header(locks, request)
        transition(locked, Status.SUBMITTED, "submit")
        if not locked.lines.exists():
            raise ValidationError(_("Add at least one line before submitting."), code="empty")
        if isinstance(header.get("notes"), str):
            header["notes"] = header["notes"].strip()
        diff = apply_changes(locked, header, SUBMIT_FIELDS)
        locked.procurement_branch_id = project.procurement_branch_id
        locked.submitted_at = db_now()
        locked.full_clean(validate_constraints=False)
        if locked.number is None:
            _assign_number(locked, locks)
        locked.save()
        _record(
            principal,
            "request.submitted",
            locked,
            procurement_branch_id=locked.procurement_branch_id,
            lines=locked.lines.count(),
            **({"changes": diff} if diff else {}),
        )
        events.publish("request.submitted", request_id=locked.pk, number=locked.number)
        return locked

    return execute_once(
        actor_id=principal.user_id,
        key=idempotency_key,
        operation="request.submit",
        payload={"request_id": request_id, **header},
        execute=execute,
        replay=lambda _type, pk: MaterialRequest.objects.get(pk=pk),
    )


@transactional
def withdraw(principal: Principal, request_id: UUID) -> MaterialRequest:
    """Take an own submitted request back to draft while no line is decided."""
    locks = LockOrder()
    request = MaterialRequest.objects.get(pk=request_id)
    _authorize(principal, WITHDRAW, request, locks)
    locked = _lock_header(locks, request)
    transition(locked, Status.DRAFT, "withdraw")
    if _decided(locked):
        raise ValidationError(
            _("A reviewer has already decided a line; it can no longer be withdrawn."),
            code="decided",
        )
    _reset_decisions(locked)
    locked.save()
    _record(principal, "request.withdrawn", locked)
    events.publish("request.withdrawn", request_id=locked.pk)
    return locked


def _cancel_path(principal: Principal, request: MaterialRequest) -> tuple[str, str]:
    """The capability and the table operation of a cancellation (plan §3.4, §4.3).

    Own draft: discarded with ``request.create``. Own submitted request with no decision:
    ``request.withdraw``. Anything else: ``request.cancel_any``.
    """
    own = request.requested_by_id == principal.user_id
    if request.status == Status.DRAFT:
        return CREATE, "discard"
    if own and request.status == Status.SUBMITTED and not _decided(request):
        return WITHDRAW, "cancel"
    return CANCEL_ANY, "cancel"


@transactional
def cancel(principal: Principal, request_id: UUID, *, reason: str = "") -> MaterialRequest:
    """Discard a draft, or cancel a submitted or approved request (nothing allocated or
    delivered). The reason is kept in the audit trail."""
    locks = LockOrder()
    request = MaterialRequest.objects.get(pk=request_id)
    capability, operation = _cancel_path(principal, request)
    _authorize(principal, capability, request, locks)
    locked = _lock_header(locks, request)
    if _cancel_path(principal, locked) != (capability, operation):
        raise LockSetChanged("A line was decided between authorization and locking")
    if locked.status == Status.APPROVED:
        _lock_lines(locks, locked)  # the allocations and handovers of its lines are checked
    left = transition(locked, Status.CANCELLED, operation)
    if left == Status.APPROVED:
        _require_nothing_allocated_or_delivered(locked)
    locked.save()
    action = "request.discarded" if operation == "discard" else "request.cancelled"
    _record(principal, action, locked, from_status=left, reason=reason.strip())
    events.publish(action, request_id=locked.pk)
    return locked
