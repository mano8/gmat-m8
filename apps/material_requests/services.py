"""Material request lifecycle, the requester's side (plan §4.3, §4.4, §4.7, §4.8).

Each service runs in one transaction with ``lock_timeout`` through the shared retry
wrapper, in the order of CLAUDE.md "Writes":

1. ``authorize_in_txn`` (L0 ``FOR SHARE``, in full, every time) on the request, with
   ``request.view`` as the read capability (404 outside it, 403 without the operation's);
   a project is authorized with ``project.view`` as its read capability;
2. the idempotency record of a create or a submit (L1);
3. the request header ``FOR UPDATE`` (L2), re-read and compared with what was authorized
   (a change in between retries the transaction: discover → lock → verify); every change
   to a line locks its header first, so a submit and an ``add_line`` serialize;
4. the line ``FOR UPDATE`` (L3) for a change to one line;
5. the document number last (L9), so a rollback leaves no gap.

Capabilities (plan §3.4): ``request.create`` (own drafts: create, edit, submit, discard;
notes of an own approved request), ``request.withdraw`` (own submitted request back to
draft, or cancelled, before any decision) and ``request.cancel_any`` (anyone's, nothing
allocated or delivered). Material, unit and quantity change only in ``DRAFT`` (§4.4).

C9: a catalog line carries its material's unit through the composite foreign key. A
material whose unit changed between reading it and inserting the line fails that key: the
line is refused (its quantity was meant in the old unit), never silently converted.
"""

from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db.models import Max
from django.utils.translation import gettext_lazy as _

from apps.access.authorization import authorize_in_txn
from apps.access.principals import Principal
from apps.access.roles import Capability
from apps.accounts.models import User
from apps.audit.recorder import record
from apps.catalog.models import Material, UnitOfMeasure
from apps.core import events
from apps.core.changes import apply_changes
from apps.core.db import constraint_errors, db_now
from apps.core.idempotency import execute_once
from apps.core.locking import LockLevel, LockOrder, LockSetChanged, transactional
from apps.core.sequences import format_number, next_number
from apps.material_requests.models import MaterialRequest, MaterialRequestLine
from apps.material_requests.transitions import transition
from apps.organizations.models import Branch
from apps.projects.models import Project

Status = MaterialRequest.Status
Decision = MaterialRequestLine.Decision

CREATE, WITHDRAW = Capability.REQUEST_CREATE, Capability.REQUEST_WITHDRAW
CANCEL_ANY, VIEW = Capability.REQUEST_CANCEL_ANY, Capability.REQUEST_VIEW

# Header fields the requester changes, per state (plan §4.4).
EDITABLE_FIELDS: Mapping[str, frozenset[str]] = {
    Status.DRAFT: frozenset({"project_id", "priority", "needed_by", "notes"}),
    Status.APPROVED: frozenset({"notes"}),
}
LINE_FIELDS = frozenset({"material_id", "free_text", "unit_id", "requested_qty"})

C9_FOREIGN_KEY = "mr_line_material_unit_fk"
LINE_CONSTRAINTS = {
    C9_FOREIGN_KEY: (
        "material",
        _("This material's unit has just changed; choose the material again."),
    ),
}


# --- authorization and locks ----------------------------------------------------------


def _authorize(
    principal: Principal, capability: str, request: MaterialRequest, locks: LockOrder
) -> User:
    return authorize_in_txn(principal, capability, request, locks=locks, read=VIEW)


def _authorize_project(principal: Principal, project: Project, locks: LockOrder) -> None:
    """``request.create`` on the project itself: new requests only where the actor may."""
    authorize_in_txn(principal, CREATE, project, locks=locks, read=Capability.PROJECT_VIEW)


def _verified(request: MaterialRequest) -> tuple[Any, ...]:
    """What an authorization and a choice of operation were based on."""
    return (
        request.project_id,
        request.procurement_branch_id,
        request.requested_by_id,
        request.status,
    )


def _lock_header(locks: LockOrder, request: MaterialRequest) -> MaterialRequest:
    """L2: lock the header and re-read it; retry if what was authorized has changed."""
    locks.lock(LockLevel.REQUEST_HEADERS, MaterialRequest, [request.pk])
    locked = MaterialRequest.objects.get(pk=request.pk)
    if _verified(locked) != _verified(request):
        raise LockSetChanged("The request changed between authorization and locking")
    return locked


def _lock_line(locks: LockOrder, line_id: UUID) -> MaterialRequestLine:
    """L3 after L2: the line's header is locked by the caller."""
    locks.lock(LockLevel.REQUEST_LINES, MaterialRequestLine, [line_id])
    return MaterialRequestLine.objects.get(pk=line_id)


def _line_request(line_id: UUID) -> MaterialRequest:
    """The request of a line; a line never moves to another request."""
    return MaterialRequest.objects.get(lines__pk=line_id)


# --- rules ----------------------------------------------------------------------------


def _project(project_id: UUID) -> Project:
    return Project.objects.select_related("branch__company", "procurement_branch__company").get(
        pk=project_id
    )


def _require_open_project(project: Project) -> None:
    """S9: only an active project of active branches and companies takes new requests."""
    if project.status != Project.Status.ACTIVE:
        raise ValidationError(
            {
                "project": ValidationError(
                    _("This project accepts no new requests."), code="project_not_active"
                )
            }
        )
    for branch in (project.branch, project.procurement_branch):
        if not (branch.is_active and branch.company.is_active):
            raise ValidationError(
                {
                    "project": ValidationError(
                        _("The project's branch is inactive."), code="inactive"
                    )
                }
            )


def _require_draft(request: MaterialRequest) -> None:
    if request.status != Status.DRAFT:
        raise ValidationError(
            _("Lines change only while the request is a draft."), code="not_draft"
        )


def _decided(request: MaterialRequest) -> bool:
    return request.lines.exclude(decision=Decision.PENDING).exists()


def _check_quantity(quantity: Any, unit: UnitOfMeasure) -> None:
    """A positive ``Decimal`` with no more decimals than the unit allows."""
    if not isinstance(quantity, Decimal) or not quantity.is_finite():
        raise ValidationError(
            {"requested_qty": ValidationError(_("Enter a quantity."), code="invalid")}
        )
    if quantity <= 0:
        raise ValidationError(
            {"requested_qty": ValidationError(_("The quantity must be positive."), code="positive")}
        )
    exponent = quantity.normalize().as_tuple().exponent
    if isinstance(exponent, int) and -exponent > unit.decimals:
        raise ValidationError(
            {
                "requested_qty": ValidationError(
                    _("%(unit)s quantities have at most %(decimals)s decimals."),
                    code="precision",
                    params={"unit": unit.code, "decimals": unit.decimals},
                )
            }
        )


def _line_values(
    *,
    material_id: UUID | None,
    free_text: str,
    unit_id: UUID | None,
    requested_qty: Any,
    new_material: bool = True,
) -> dict[str, Any]:
    """Validated line content. A catalog line takes its material's unit (C9).

    ``new_material``: the material is being chosen now, so it must be active; a line
    keeps a material deactivated after it was added.
    """
    free_text = free_text.strip()
    if material_id is not None:
        material = Material.objects.select_related("unit").filter(pk=material_id).first()
        if material is None or (new_material and not material.is_active):
            raise ValidationError(
                {"material": ValidationError(_("Choose an active material."), code="inactive")}
            )
        if unit_id is not None and unit_id != material.unit_id:
            raise ValidationError(
                {
                    "unit": ValidationError(
                        _("A catalog material is requested in its own unit."), code="unit_mismatch"
                    )
                }
            )
        unit = material.unit
    else:
        if not free_text:
            raise ValidationError(
                {
                    "free_text": ValidationError(
                        _("Describe the material, or choose one from the catalog."),
                        code="required",
                    )
                }
            )
        found = UnitOfMeasure.objects.filter(pk=unit_id).first() if unit_id is not None else None
        if found is None:
            raise ValidationError({"unit": ValidationError(_("Choose a unit."), code="required")})
        unit = found
    _check_quantity(requested_qty, unit)
    return {
        "material_id": material_id,
        "free_text": free_text,
        "unit_id": unit.pk,
        "requested_qty": requested_qty,
    }


def _save_line(line: MaterialRequestLine) -> None:
    line.full_clean(validate_constraints=False)
    with constraint_errors(LINE_CONSTRAINTS):
        line.save()


def _line_data(line: MaterialRequestLine) -> dict[str, Any]:
    return {
        "line_no": line.line_no,
        "material_id": line.material_id,
        "free_text": line.free_text,
        "unit_id": line.unit_id,
        "requested_qty": line.requested_qty,
    }


def _owner_code(branch: Branch) -> str:
    """``A-MAD``: branch codes are unique per company only."""
    return f"{branch.company.code}-{branch.code}"


def _assign_number(request: MaterialRequest, locks: LockOrder) -> None:
    """``SOL-{company}-{branch}-{yyyy}-{00001}`` from the procurement branch (L9, last)."""
    branch = Branch.objects.select_related("company").get(pk=request.procurement_branch_id)
    owner, year = _owner_code(branch), db_now().year
    locks.enter(LockLevel.DOCUMENT_SEQUENCE)
    value = next_number(f"request:{owner}:{year}")
    request.number = format_number(settings.REQUEST_NUMBER_PREFIX, owner, year, value)


def _reset_decisions(request: MaterialRequest) -> None:
    """Back to ``DRAFT`` clears every decision (plan §4.4, invalidation); audit keeps them."""
    request.lines.update(
        decision=Decision.PENDING,
        approved_qty=None,
        decided_by=None,
        decided_at=None,
        decision_note="",
        sourcing=MaterialRequestLine.Sourcing.UNDECIDED,
    )
    request.decided_at = None


def _require_nothing_allocated_or_delivered(request: MaterialRequest) -> None:
    """A cancellation needs Σ allocations = 0 and nothing delivered (plan §4.9).

    Purchasing (M5.1) and handovers (M6.2) add their checks here; until they exist
    nothing can be allocated or delivered.
    """


def _record(principal: Principal, action: str, request: MaterialRequest, **data: Any) -> None:
    record(
        principal.audit_identity,
        action,
        obj=request,
        branch_id=request.procurement_branch_id,
        data={"number": request.number, **data},
    )


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
    principal: Principal, request_id: UUID, *, idempotency_key: str | None = None
) -> MaterialRequest:
    """Send a draft for review: take the procurement-branch snapshot, assign the number.

    The number is taken at the first submit and kept if the request comes back to draft.
    With an idempotency key a repeated submit (a double tap) returns the request again.
    """
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
        locked.procurement_branch_id = project.procurement_branch_id
        locked.submitted_at = db_now()
        if locked.number is None:
            _assign_number(locked, locks)
        locked.save()
        _record(
            principal,
            "request.submitted",
            locked,
            procurement_branch_id=locked.procurement_branch_id,
            lines=locked.lines.count(),
        )
        events.publish("request.submitted", request_id=locked.pk, number=locked.number)
        return locked

    return execute_once(
        actor_id=principal.user_id,
        key=idempotency_key,
        operation="request.submit",
        payload={"request_id": request_id},
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
    left = transition(locked, Status.CANCELLED, operation)
    if left == Status.APPROVED:
        _require_nothing_allocated_or_delivered(locked)
    locked.save()
    action = "request.discarded" if operation == "discard" else "request.cancelled"
    _record(principal, action, locked, from_status=left, reason=reason.strip())
    events.publish(action, request_id=locked.pk)
    return locked
