"""Material request lifecycle: requester, reviewer and material manager (plan §4.3 to §4.9).

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
draft, or cancelled, before any decision), ``request.cancel_any`` (anyone's, nothing
allocated or delivered), ``request.review`` (decide lines, return for changes, reduce an
approved quantity; never on one's own request unless the procurement branch allows
self-approval) and ``request.source`` (sourcing, non-catalog resolution, priority and
needed-by, closing and reopening). Material, unit and quantity change only in ``DRAFT``
(§4.4); every request goes through review (Q4, ADR-0010), with no automatic approval.

Ledger inputs (§4.5): closing or reopening a line or a request, reducing an approved
quantity and changing the priority or needed-by date change the attribution order or the
open lines, so they lock every line of the request (L3) and end in ``_reconcile``, where
M5.1 adds the allocation trims and the component reconciliation. The quantities they
check (D, R, P, Q) come from ``ledger``; automatic close and reopen follow §4.9.

C9: a catalog line carries its material's unit through the composite foreign key. A
material whose unit changed between reading it and inserting the line fails that key: the
line is refused (its quantity was meant in the old unit), never silently converted.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from types import EllipsisType
from typing import Any
from uuid import UUID

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db.models import Max
from django.utils.translation import gettext_lazy as _

from apps.access.authorization import authorize_in_txn
from apps.access.policy import require_not_self_approval
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
from apps.material_requests import ledger
from apps.material_requests.models import MaterialRequest, MaterialRequestLine
from apps.material_requests.transitions import transition
from apps.organizations.models import Branch
from apps.projects.models import Project

Status = MaterialRequest.Status
Decision = MaterialRequestLine.Decision
Sourcing = MaterialRequestLine.Sourcing
Closure = MaterialRequestLine.ClosedShortSource

CREATE, WITHDRAW = Capability.REQUEST_CREATE, Capability.REQUEST_WITHDRAW
CANCEL_ANY, VIEW = Capability.REQUEST_CANCEL_ANY, Capability.REQUEST_VIEW
REVIEW, SOURCE = Capability.REQUEST_REVIEW, Capability.REQUEST_SOURCE

# Header fields the requester changes, per state (plan §4.4).
EDITABLE_FIELDS: Mapping[str, frozenset[str]] = {
    Status.DRAFT: frozenset({"project_id", "priority", "needed_by", "notes"}),
    Status.APPROVED: frozenset({"notes"}),
}
LINE_FIELDS = frozenset({"material_id", "free_text", "unit_id", "requested_qty"})
# The details the review step of the requester's flow sets together with the submit.
SUBMIT_FIELDS = frozenset({"priority", "needed_by", "notes"})

# The closure fields of a line that is not closed short (CHECK ``mr_line_closure_fields``).
NO_CLOSURE: Mapping[str, Any] = {
    "closed_short_at": None,
    "closed_short_by": None,
    "closed_short_source": "",
    "close_reason": "",
}

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


def _check_quantity(
    quantity: Any, unit: UnitOfMeasure, field: str = "requested_qty", *, zero: bool = False
) -> None:
    """A positive ``Decimal`` (or zero, with ``zero``) with no more decimals than the unit
    allows."""
    if not isinstance(quantity, Decimal) or not quantity.is_finite():
        raise ValidationError({field: ValidationError(_("Enter a quantity."), code="invalid")})
    if quantity < 0 or (quantity == 0 and not zero):
        raise ValidationError(
            {field: ValidationError(_("The quantity must be positive."), code="positive")}
        )
    exponent = quantity.normalize().as_tuple().exponent
    if isinstance(exponent, int) and -exponent > unit.decimals:
        raise ValidationError(
            {
                field: ValidationError(
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
    """Back to ``DRAFT`` clears every decision (plan §4.4, invalidation); audit keeps them.

    Line closures belong to the approval too, so they go with it.
    """
    request.lines.update(
        decision=Decision.PENDING,
        approved_qty=None,
        decided_by=None,
        decided_at=None,
        decision_note="",
        sourcing=Sourcing.UNDECIDED,
        **NO_CLOSURE,
    )
    request.decided_at = None


def _require_nothing_allocated_or_delivered(request: MaterialRequest) -> None:
    """Σ allocations = 0 on draft or issued orders and nothing delivered (plan §4.9).

    For a cancellation or a return to draft of an approved request. The quantities come
    from the purchasing (M5.1) and handover (M6.2) flow sources.
    """
    found = ledger.flows(list(request.lines.values_list("pk", flat=True)))
    if any(flow.allocated > 0 or flow.delivered > 0 for flow in found.values()):
        raise ValidationError(
            _("Material has already been ordered or delivered for this request."),
            code="in_progress",
        )


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
    left = transition(locked, Status.CANCELLED, operation)
    if left == Status.APPROVED:
        _require_nothing_allocated_or_delivered(locked)
    locked.save()
    action = "request.discarded" if operation == "discard" else "request.cancelled"
    _record(principal, action, locked, from_status=left, reason=reason.strip())
    events.publish(action, request_id=locked.pk)
    return locked


# --- review and sourcing: shared rules ------------------------------------------------

SOURCINGS = frozenset({Sourcing.STOCK, Sourcing.PURCHASE})


def _require_state(request: MaterialRequest, *states: str) -> None:
    if request.status not in states:
        raise ValidationError(
            _("This request is %(status)s; it cannot be changed this way."),
            code="transition",
            params={"status": Status(request.status).label},
        )


def _reason(text: str, field: str = "reason") -> str:
    """A required reason or note, stripped."""
    text = text.strip()
    if not text:
        raise ValidationError({field: ValidationError(_("Give a reason."), code="required")})
    return text


def _require_live(line: MaterialRequestLine) -> None:
    """Only a live line (approved, quantity > 0, not closed short) takes this change."""
    if line.closed_short_at is not None:
        raise ValidationError(_("This line is closed short."), code="closed_short")
    if not ledger.is_live(line):
        raise ValidationError(_("This line has no approved quantity."), code="not_live")


def _lock_lines(
    locks: LockOrder, request: MaterialRequest, line_ids: list[UUID] | None = None
) -> list[MaterialRequestLine]:
    """L3 after L2: these lines of the locked request (all by default), in line order.

    Outside ``DRAFT`` no line is added or removed, and never without the header lock, so
    the set read under that lock is the set locked.
    """
    lines = request.lines.select_related("unit")
    ids = list(lines.values_list("pk", flat=True)) if line_ids is None else line_ids
    locks.lock(LockLevel.REQUEST_LINES, MaterialRequestLine, ids)
    return list(lines.filter(pk__in=ids))


def _locked_line(
    principal: Principal, capability: str, line_id: UUID, locks: LockOrder
) -> tuple[User, MaterialRequest, list[MaterialRequestLine], MaterialRequestLine]:
    """Authorize on the line's request, lock it (L2) and all its lines (L3).

    Returns the actor, the request, its lines and the one asked for.
    """
    request = _line_request(line_id)
    actor = _authorize(principal, capability, request, locks)
    locked = _lock_header(locks, request)
    lines = _lock_lines(locks, locked)
    line = next(item for item in lines if item.pk == line_id)
    return actor, locked, lines, line


def _record_line(
    principal: Principal,
    action: str,
    request: MaterialRequest,
    line: MaterialRequestLine,
    **data: Any,
) -> None:
    _record(principal, action, request, line_id=line.pk, line_no=line.line_no, **data)
    events.publish(action, request_id=request.pk, line_id=line.pk)


def _close_line(line: MaterialRequestLine, actor: User, source: str, reason: str, now: Any) -> None:
    line.closed_short_at = now
    line.closed_short_by = actor
    line.closed_short_source = source
    line.close_reason = reason
    line.save()


def _reopen_line(line: MaterialRequestLine) -> None:
    for name, value in NO_CLOSURE.items():
        setattr(line, name, value)
    line.save()


def _reconcile(locks: LockOrder, request: MaterialRequest, cause: str) -> None:
    """Restore I2 over the request's component after a ledger input changed (§4.5).

    M5.1 wires the allocation trims of a closure and ``reconcile_component`` in here, and
    widens the locks to the whole component before L3. Until purchase orders exist nothing
    is allocated, so no line can be committed beyond its approved quantity.
    """


def _settle_closure(
    principal: Principal, request: MaterialRequest, lines: list[MaterialRequestLine]
) -> None:
    """Automatic close and reopen (§4.9), after an operation that changed D or A.

    ``APPROVED → CLOSED`` when every live line is delivered; ``CLOSED → APPROVED`` when the
    close was automatic and a live line is short again. Both are recorded as the actor's.
    """
    ledgers = ledger.line_ledgers(lines)
    if request.status == Status.APPROVED and ledger.auto_close_eligible(ledgers):
        transition(request, Status.CLOSED, "auto_close")
        request.closed_at = db_now()
        request.close_mode = MaterialRequest.CloseMode.AUTO
        request.save()
        action = "request.auto_closed"
    elif request.status == Status.CLOSED and ledger.auto_reopen_eligible(request, ledgers):
        transition(request, Status.APPROVED, "auto_reopen")
        request.closed_at, request.close_mode, request.close_reason = None, "", ""
        request.save()
        action = "request.auto_reopened"
    else:
        return
    _record(principal, action, request)
    events.publish(action, request_id=request.pk)


# --- review ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LineDecision:
    """A reviewer's decision on one line: approve (``approved_qty``, the requested quantity
    by default) or reject (``note`` required)."""

    line_id: UUID
    decision: str
    approved_qty: Decimal | None = None
    note: str = ""


def _decision_values(decision: LineDecision, line: MaterialRequestLine) -> dict[str, Any]:
    if decision.decision == Decision.APPROVED:
        quantity = line.requested_qty if decision.approved_qty is None else decision.approved_qty
        _check_quantity(quantity, line.unit, "approved_qty")
        if quantity > line.requested_qty:
            raise ValidationError(
                {
                    "approved_qty": ValidationError(
                        _("At most the requested quantity can be approved."),
                        code="above_requested",
                    )
                }
            )
        return {
            "decision": Decision.APPROVED,
            "approved_qty": quantity,
            "decision_note": decision.note.strip(),
        }
    if decision.decision == Decision.REJECTED:
        return {
            "decision": Decision.REJECTED,
            "approved_qty": None,
            "decision_note": _reason(decision.note, "decision_note"),
        }
    raise ValidationError(
        {"decision": ValidationError(_("Approve or reject the line."), code="invalid")}
    )


DECISION_FIELDS = frozenset({"decision", "approved_qty", "decision_note"})


def _decide(
    principal: Principal,
    actor: User,
    request: MaterialRequest,
    lines: list[MaterialRequestLine],
    decisions: list[LineDecision],
) -> None:
    """Apply the decisions to the locked lines, then finalise the request if all are decided.

    A reviewer changes only their own decisions (§4.4); a line someone else decided stays
    theirs until the request returns to draft.
    """
    by_id = {line.pk: line for line in lines}
    now = db_now()
    for decision in decisions:
        line = by_id[decision.line_id]
        if line.decision != Decision.PENDING and line.decided_by_id != actor.pk:
            raise ValidationError(
                _("Line %(line)s was decided by another reviewer."),
                code="decided_by_other",
                params={"line": line.line_no},
            )
        diff = apply_changes(line, _decision_values(decision, line), DECISION_FIELDS)
        if not diff:
            continue
        line.decided_by, line.decided_at = actor, now
        line.save()
        _record_line(principal, "request_line.decided", request, line, changes=diff)
    decided = {line.decision for line in request.lines.all()}
    if Decision.PENDING in decided:
        return
    approved = Decision.APPROVED in decided
    transition(request, Status.APPROVED if approved else Status.REJECTED, "finalize")
    request.decided_at = now
    request.save()
    action = "request.approved" if approved else "request.rejected"
    _record(principal, action, request)
    events.publish(action, request_id=request.pk)


def _review_lock(
    principal: Principal, request_id: UUID, locks: LockOrder
) -> tuple[User, MaterialRequest]:
    """Authorize ``request.review``, lock the header, require ``SUBMITTED`` and no
    self-approval (the procurement branch decides whether its members may)."""
    request = MaterialRequest.objects.get(pk=request_id)
    actor = _authorize(principal, REVIEW, request, locks)
    locked = _lock_header(locks, request)
    _require_state(locked, Status.SUBMITTED)
    branch = Branch.objects.get(pk=locked.procurement_branch_id)
    require_not_self_approval(actor.pk, locked.requested_by_id, branch)
    return actor, locked


@transactional
def decide_lines(
    principal: Principal, request_id: UUID, decisions: list[LineDecision]
) -> MaterialRequest:
    """Decide some lines of a submitted request in one step (the review page).

    Approving takes at most the requested quantity (partial approval keeps both values);
    rejecting needs a note. When the last line is decided the request becomes ``APPROVED``
    (at least one line approved) or ``REJECTED``.
    """
    ids = [decision.line_id for decision in decisions]
    if len(set(ids)) != len(ids):
        raise ValueError("Each line is decided once per call")
    if not ids:
        raise ValidationError(_("Decide at least one line."), code="empty")
    locks = LockOrder()
    actor, locked = _review_lock(principal, request_id, locks)
    if locked.lines.filter(pk__in=ids).count() != len(ids):
        raise ValidationError(_("A line does not belong to this request."), code="unknown_line")
    lines = _lock_lines(locks, locked, ids)
    _decide(principal, actor, locked, lines, decisions)
    return locked


def decide_line(
    principal: Principal,
    line_id: UUID,
    *,
    decision: str,
    approved_qty: Decimal | None = None,
    note: str = "",
) -> MaterialRequestLine:
    """Approve (at most the requested quantity) or reject (with a note) one line."""
    request = _line_request(line_id)
    decide_lines(principal, request.pk, [LineDecision(line_id, decision, approved_qty, note)])
    return MaterialRequestLine.objects.get(pk=line_id)


@transactional
def approve_all(principal: Principal, request_id: UUID, *, note: str = "") -> MaterialRequest:
    """Approve every undecided line at its requested quantity, then finalise."""
    locks = LockOrder()
    actor, locked = _review_lock(principal, request_id, locks)
    lines = _lock_lines(locks, locked)
    pending = [line for line in lines if line.decision == Decision.PENDING]
    decisions = [LineDecision(line.pk, Decision.APPROVED, note=note) for line in pending]
    _decide(principal, actor, locked, pending, decisions)
    return locked


@transactional
def return_to_draft(principal: Principal, request_id: UUID, *, note: str) -> MaterialRequest:
    """Send a submitted or approved request back to its requester with a note.

    Every decision is cleared (the audit keeps them). An approved request returns only
    while nothing is allocated or delivered.
    """
    note = _reason(note, "note")
    locks = LockOrder()
    request = MaterialRequest.objects.get(pk=request_id)
    _authorize(principal, REVIEW, request, locks)
    locked = _lock_header(locks, request)
    _lock_lines(locks, locked)
    left = transition(locked, Status.DRAFT, "return_to_draft")
    if left == Status.APPROVED:
        _require_nothing_allocated_or_delivered(locked)
    _reset_decisions(locked)
    locked.save()
    _record(principal, "request.returned", locked, from_status=left, note=note)
    events.publish("request.returned", request_id=locked.pk)
    return locked


@transactional
def reduce_approved_qty(
    principal: Principal, line_id: UUID, *, approved_qty: Decimal, reason: str = ""
) -> MaterialRequestLine:
    """Lower the approved quantity of a live line of an approved request.

    Never below max(D, R) + P + Q (§4.4, F70), never on a closed-short line, never up:
    more takes a new request. A line reduced to zero is no longer live.
    """
    locks = LockOrder()
    _actor, locked, lines, line = _locked_line(principal, REVIEW, line_id, locks)
    _require_state(locked, Status.APPROVED)
    _require_live(line)
    _check_quantity(approved_qty, line.unit, "approved_qty", zero=True)
    (entry,) = ledger.line_ledgers([line])
    current = entry.approved
    if approved_qty > current:
        raise ValidationError(
            {
                "approved_qty": ValidationError(
                    _("An approved quantity can only be reduced."), code="not_lower"
                )
            }
        )
    if approved_qty == current:
        return line
    floor = entry.floor
    if approved_qty < floor:
        raise ValidationError(
            {
                "approved_qty": ValidationError(
                    _("%(floor)s are already delivered, received or on order."),
                    code="below_commitment",
                    params={"floor": floor},
                )
            }
        )
    line.approved_qty = approved_qty
    line.save()
    _record_line(
        principal,
        "request_line.reduced",
        locked,
        line,
        changes={"approved_qty": {"from": current, "to": approved_qty}},
        reason=reason.strip(),
    )
    _reconcile(locks, locked, "reduce_approved_qty")
    _settle_closure(principal, locked, lines)
    return line


# --- sourcing and schedule ------------------------------------------------------------


@transactional
def set_sourcing(principal: Principal, line_id: UUID, *, sourcing: str) -> MaterialRequestLine:
    """Choose how an open line is supplied: from stock or by purchase (§4.4).

    ``PURCHASE`` needs a catalog material; ``PURCHASE → STOCK`` only with nothing pending
    receipt and nothing reserved in draft orders.
    """
    if sourcing not in SOURCINGS:
        raise ValidationError(
            {"sourcing": ValidationError(_("Choose stock or purchase."), code="invalid")}
        )
    locks = LockOrder()
    request = _line_request(line_id)
    _authorize(principal, SOURCE, request, locks)
    locked = _lock_header(locks, request)
    _require_state(locked, Status.APPROVED)
    (line,) = _lock_lines(locks, locked, [line_id])
    _require_live(line)
    previous = line.sourcing
    if sourcing == previous:
        return line
    if sourcing == Sourcing.PURCHASE and line.material_id is None:
        raise ValidationError(
            _("Link the line to a catalog material before purchasing it."), code="needs_material"
        )
    if previous == Sourcing.PURCHASE:
        flow = ledger.flows([line.pk])[line.pk]
        if flow.pending > 0 or flow.reserved > 0:
            raise ValidationError(
                _("Goods are on order for this line; it cannot move to stock."),
                code="purchase_in_progress",
            )
    line.sourcing = sourcing
    line.save()
    _record_line(
        principal,
        "request_line.sourcing_set",
        locked,
        line,
        changes={"sourcing": {"from": previous, "to": sourcing}},
    )
    return line


@transactional
def resolve_non_catalog_line(
    principal: Principal, line_id: UUID, *, material_id: UUID
) -> MaterialRequestLine:
    """Link an open non-catalog line to an active material in the line's unit (C9).

    The description stays as the requester wrote it; the quantity was meant in the line's
    unit, so a material in another unit is refused (no conversions, Q15).
    """
    locks = LockOrder()
    request = _line_request(line_id)
    _authorize(principal, SOURCE, request, locks)
    locked = _lock_header(locks, request)
    _require_state(locked, Status.APPROVED)
    (line,) = _lock_lines(locks, locked, [line_id])
    _require_live(line)
    if line.material_id is not None:
        raise ValidationError(_("This line is already a catalog material."), code="catalog_line")
    material = Material.objects.filter(pk=material_id, is_active=True).first()
    if material is None:
        raise ValidationError(
            {"material": ValidationError(_("Choose an active material."), code="inactive")}
        )
    if material.unit_id != line.unit_id:
        raise ValidationError(
            {
                "material": ValidationError(
                    _("This material is measured in another unit."), code="unit_mismatch"
                )
            }
        )
    line.material = material
    _save_line(line)
    _record_line(principal, "request_line.resolved", locked, line, material_id=material.pk)
    return line


@transactional
def change_schedule(
    principal: Principal,
    request_id: UUID,
    *,
    priority: str | None = None,
    needed_by: date | EllipsisType | None = ...,
) -> MaterialRequest:
    """Change an approved request's priority and/or needed-by date (``...``: unchanged).

    Both decide the attribution order of receipts and pending (§4.5), so the change is
    reconciled.
    """
    changes: dict[str, Any] = {}
    if priority is not None:
        changes["priority"] = priority
    if needed_by is not ...:
        changes["needed_by"] = needed_by
    locks = LockOrder()
    request = MaterialRequest.objects.get(pk=request_id)
    _authorize(principal, SOURCE, request, locks)
    locked = _lock_header(locks, request)
    _lock_lines(locks, locked)
    _require_state(locked, Status.APPROVED)
    diff = apply_changes(locked, changes, {"priority", "needed_by"})
    if not diff:
        return locked
    locked.full_clean(validate_constraints=False)
    locked.save()
    _record(principal, "request.rescheduled", locked, changes=diff)
    events.publish("request.rescheduled", request_id=locked.pk, fields=sorted(diff))
    _reconcile(locks, locked, "schedule")
    return locked


def change_priority(principal: Principal, request_id: UUID, *, priority: str) -> MaterialRequest:
    return change_schedule(principal, request_id, priority=priority)


def change_needed_by(
    principal: Principal, request_id: UUID, *, needed_by: date | None
) -> MaterialRequest:
    return change_schedule(principal, request_id, needed_by=needed_by)


# --- closing and reopening ------------------------------------------------------------


@transactional
def close_line_short(principal: Principal, line_id: UUID, *, reason: str) -> MaterialRequestLine:
    """Give up the rest of a live line (source ``LINE``); its allocations are trimmed.

    The request closes automatically once no live line is short of delivery (§4.9).
    """
    reason = _reason(reason)
    locks = LockOrder()
    actor, locked, lines, line = _locked_line(principal, SOURCE, line_id, locks)
    _require_state(locked, Status.APPROVED)
    _require_live(line)
    _close_line(line, actor, Closure.LINE, reason, db_now())
    _record_line(principal, "request_line.closed_short", locked, line, reason=reason)
    _reconcile(locks, locked, "close_line_short")
    _settle_closure(principal, locked, lines)
    return line


@transactional
def reopen_line(principal: Principal, line_id: UUID, *, reason: str) -> MaterialRequestLine:
    """Reopen a line closed on its own (``LINE``); it keeps its trimmed allocations (E5)."""
    reason = _reason(reason)
    locks = LockOrder()
    _actor, locked, _lines, line = _locked_line(principal, SOURCE, line_id, locks)
    _require_state(locked, Status.APPROVED)
    if line.closed_short_source != Closure.LINE:
        raise ValidationError(_("This line is not closed short."), code="not_closed")
    closed = {"closed_short_at": line.closed_short_at, "close_reason": line.close_reason}
    _reopen_line(line)
    _record_line(principal, "request_line.reopened", locked, line, reason=reason, closed=closed)
    _reconcile(locks, locked, "reopen_line")
    return line


@transactional
def manual_close(principal: Principal, request_id: UUID, *, reason: str) -> MaterialRequest:
    """Close an approved request: every still-live line is closed short (source ``REQUEST``)
    and its allocations trimmed (§4.9)."""
    reason = _reason(reason)
    locks = LockOrder()
    request = MaterialRequest.objects.get(pk=request_id)
    actor = _authorize(principal, SOURCE, request, locks)
    locked = _lock_header(locks, request)
    lines = _lock_lines(locks, locked)
    transition(locked, Status.CLOSED, "manual_close")
    now = db_now()
    closed = [line for line in lines if ledger.is_live(line)]
    for line in closed:
        _close_line(line, actor, Closure.REQUEST, reason, now)
    locked.closed_at, locked.close_mode, locked.close_reason = (
        now,
        MaterialRequest.CloseMode.MANUAL,
        reason,
    )
    locked.save()
    _record(
        principal, "request.closed", locked, reason=reason, lines=[line.line_no for line in closed]
    )
    events.publish("request.closed", request_id=locked.pk)
    _reconcile(locks, locked, "manual_close")
    return locked


@transactional
def reopen(principal: Principal, request_id: UUID, *, reason: str) -> MaterialRequest:
    """Reopen a closed request (automatic or manual close) with a reason.

    Only the lines its manual close closed (``REQUEST``) reopen; lines closed on their own
    (``LINE``) stay closed until ``reopen_line``. Reopened lines keep their trimmed
    allocations, so given-up commitments never revive (E5).
    """
    reason = _reason(reason)
    locks = LockOrder()
    request = MaterialRequest.objects.get(pk=request_id)
    _authorize(principal, SOURCE, request, locks)
    locked = _lock_header(locks, request)
    lines = _lock_lines(locks, locked)
    transition(locked, Status.APPROVED, "reopen")
    reopened = [line for line in lines if line.closed_short_source == Closure.REQUEST]
    for line in reopened:
        _reopen_line(line)
    close_mode = locked.close_mode
    locked.closed_at, locked.close_mode, locked.close_reason = None, "", ""
    locked.save()
    _record(
        principal,
        "request.reopened",
        locked,
        reason=reason,
        close_mode=close_mode,
        lines=[line.line_no for line in reopened],
    )
    events.publish("request.reopened", request_id=locked.pk)
    _reconcile(locks, locked, "reopen")
    return locked
