"""Reviewer and material manager side: decisions, sourcing, schedule, closing (plan §4.9)."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from types import EllipsisType
from typing import Any
from uuid import UUID

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.access.policy import require_not_self_approval
from apps.access.principals import Principal
from apps.accounts.models import User
from apps.catalog.models import Material
from apps.core import events
from apps.core.changes import apply_changes
from apps.core.db import db_now
from apps.core.locking import LockOrder, transactional
from apps.material_requests import ledger
from apps.material_requests.models import MaterialRequest, MaterialRequestLine
from apps.material_requests.services.common import (
    REVIEW,
    SOURCE,
    SOURCINGS,
    Closure,
    Decision,
    Sourcing,
    Status,
    _authorize,
    _check_quantity,
    _close_line,
    _line_request,
    _lock_header,
    _lock_lines,
    _locked_line,
    _reason,
    _reconcile,
    _record,
    _record_line,
    _reopen_line,
    _require_live,
    _require_nothing_allocated_or_delivered,
    _require_state,
    _reset_decisions,
    _save_line,
    _settle_closure,
)
from apps.material_requests.transitions import transition
from apps.organizations.models import Branch

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
                    params={"floor": f"{floor.normalize():f}"},
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
    (entry,) = ledger.line_ledgers([line])
    if entry.fully_delivered:
        raise ValidationError(
            _("This line is fully delivered: nothing is short."), code="fully_delivered"
        )
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


def _short(entry: ledger.LineLedger) -> bool:
    """A live line with something still to deliver."""
    return ledger.is_live(entry.line) and not entry.fully_delivered


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
    # A fully delivered line has nothing short: it is not marked closed short.
    closed = [entry.line for entry in ledger.line_ledgers(lines) if _short(entry)]
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
