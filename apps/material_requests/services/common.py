"""Shared rules of the material request services: authorization, locks, editing rules."""

from collections.abc import Mapping
from decimal import Decimal
from typing import Any
from uuid import UUID

from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.access.authorization import authorize_in_txn
from apps.access.principals import Principal
from apps.access.roles import Capability
from apps.accounts.models import User
from apps.audit.recorder import record
from apps.catalog.models import Material, UnitOfMeasure
from apps.core import events
from apps.core.db import constraint_errors, db_now
from apps.core.locking import LockLevel, LockOrder, LockSetChanged
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
