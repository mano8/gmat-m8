"""Reviewer edits of a submitted or approved request (M7b.3, Q32-Q34, Q36, ADR-0014 §5).

``request.edit`` holders (group administrators, material managers, engineers) change a
line's material, description, unit or quantity (``edit_line``), add a line
(``add_line_as_reviewer``) or remove one (``remove_line_as_reviewer``), always with a reason.
Each call is one ``MaterialRequestLineRevision`` against the request's latest as-submitted
copy and follows the write order of plan §4.7: ``authorize_in_txn`` → idempotency → the
request's allocation component (``commitments``, L2-L5) before any line → validate →
write → automatic close or reopen → reconcile → audit and event.

By state:

- ``SUBMITTED``: an edit clears the line's decision, whoever made it. The editor may decide
  the edited line in the same call (``LineEdit.decision``): that takes ``request.review``
  and the self-decision rule (Q33) like a decision on the review page, and finalises the
  request when no line is left undecided. Removing a line rejects it with the reason.
- ``APPROVED``: the editor's edit is approved with it (Q33); only a live line is edited.
  A new quantity becomes both the requested and the approved quantity, never below
  max(D, R) + P + Q (§4.5). A material or unit change (or a new description of a
  non-catalog line) happens in place only while the line has no live allocation and
  nothing received or delivered; it clears the line's sourcing, decided for the old
  material. Otherwise the edit is a ``REPLACE`` (Q36): the line is closed short
  (``LINE``, the reason as close reason, allocations trimmed to what they received) and
  an approved line with the new material takes the rest, A - max(D, R) unless the edit
  gives a quantity (it must when the unit changes). Removing a line closes it short;
  an added line is approved at once.
- ``DRAFT``, ``CLOSED``, ``REJECTED`` and ``CANCELLED`` refuse every edit (reopen first).

A new line (``ADD``, ``REPLACE``) needs an open project (S9, C10). The requester hears of
the edits through the ``request.edited`` event (``apps.notifications.notices``), unless
they made them. ``seen_version`` (``revisions.version``) refuses an edit made on a page
older than another edit or a resubmit.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.access.principals import Principal
from apps.accounts.models import User
from apps.catalog.models import UnitOfMeasure
from apps.catalog.quantities import check_quantity
from apps.core import events
from apps.core.changes import apply_changes
from apps.core.db import db_now
from apps.core.idempotency import execute_once
from apps.core.locking import LockOrder, transactional
from apps.material_requests import commitments, ledger, revisions
from apps.material_requests.commitments import LockedComponent
from apps.material_requests.models import (
    MaterialRequest,
    MaterialRequestBaseline,
    MaterialRequestLine,
    MaterialRequestLineRevision,
)
from apps.material_requests.services.common import (
    EDIT,
    LINE_FIELDS,
    REVIEW,
    Closure,
    Decision,
    Sourcing,
    Status,
    _authorize,
    _close_line,
    _line_content,
    _line_request,
    _line_values,
    _lock_component,
    _project,
    _reason,
    _record_baseline,
    _record_line,
    _require_live,
    _require_open_project,
    _require_state,
    _require_version,
    _save_line,
    _settle_closure,
)
from apps.material_requests.services.review import (
    DECISION_FIELDS,
    LineDecision,
    _decision_values,
    _finalise,
    _require_may_decide,
    _self_decided,
)

Kind = MaterialRequestLineRevision.Kind

ACTIONS = {
    Kind.CHANGE: "request_line.edited",
    Kind.ADD: "request_line.added_by_reviewer",
    Kind.REMOVE: "request_line.removed_by_reviewer",
    Kind.REPLACE: "request_line.replaced",
}
DECISIONS = frozenset({Decision.APPROVED, Decision.REJECTED})
CONTENT_FIELDS = ("material_id", "free_text", "unit_id")


@dataclass(frozen=True)
class LineEdit:
    """A reviewer's edit of one line, or the content of a line they add.

    ``changes``: some of ``material_id``, ``free_text``, ``unit_id`` and ``requested_qty``
    (a new line needs its material or description, unit and quantity; a catalog line's
    unit follows its material). ``decision`` (``APPROVED`` or ``REJECTED``) and
    ``approved_qty``: the editor's decision on the edited line, in ``SUBMITTED`` only.
    ``seen_version``: ``revisions.version`` of the page the edit was made on.
    """

    reason: str
    changes: Mapping[str, Any] = field(default_factory=dict)
    decision: str = ""
    approved_qty: Decimal | None = None
    seen_version: int | None = None

    def payload(self) -> dict[str, Any]:
        """The command, for its idempotency hash."""
        return {
            "reason": self.reason,
            "changes": dict(self.changes),
            "decision": self.decision,
            "approved_qty": self.approved_qty,
            "seen_version": self.seen_version,
        }


@dataclass
class _Editing:
    """One edit under its locks: who, on what, and what has been written so far."""

    principal: Principal
    actor: User
    request: MaterialRequest
    lines: list[MaterialRequestLine]
    component: LockedComponent
    baseline: MaterialRequestBaseline
    reason: str
    now: datetime

    def line(self, line_id: UUID) -> MaterialRequestLine:
        return next(line for line in self.lines if line.pk == line_id)


# --- the three services ---------------------------------------------------------------


@transactional
def edit_line(
    principal: Principal, line_id: UUID, edit: LineEdit, *, idempotency_key: str | None = None
) -> MaterialRequestLineRevision:
    """Change a line's material, description, unit or quantity (see the module note)."""
    _check_fields(edit.changes)
    reason = _reason(edit.reason)
    locks = LockOrder()
    request = _line_request(line_id)
    actor = _authorize_edit(principal, request, locks, edit)

    def execute() -> MaterialRequestLineRevision:
        editing = _lock(principal, actor, request, locks, edit, reason)
        line = editing.line(line_id)
        if editing.request.status == Status.SUBMITTED:
            revision = _change_submitted(editing, line, edit)
        else:
            revision = _change_approved(editing, line, edit)
        _finish(editing, revision, "edit_line")
        return revision

    return execute_once(
        actor_id=principal.user_id,
        key=idempotency_key,
        operation="request_line.edit",
        payload={"line_id": line_id, **edit.payload()},
        execute=execute,
        replay=_replay,
    )


@transactional
def add_line_as_reviewer(
    principal: Principal, request_id: UUID, edit: LineEdit, *, idempotency_key: str | None = None
) -> MaterialRequestLineRevision:
    """Add a line to a submitted (pending, or decided by the editor) or approved
    (approved at once) request."""
    _check_fields(edit.changes)
    reason = _reason(edit.reason)
    locks = LockOrder()
    request = MaterialRequest.objects.get(pk=request_id)
    actor = _authorize_edit(principal, request, locks, edit)

    def execute() -> MaterialRequestLineRevision:
        editing = _lock(principal, actor, request, locks, edit, reason)
        changes = edit.changes
        values = _line_values(
            material_id=changes.get("material_id"),
            free_text=changes.get("free_text", ""),
            unit_id=changes.get("unit_id"),
            requested_qty=changes.get("requested_qty"),
        )
        _require_open_project(_project(editing.request.project_id))
        approved = editing.request.status == Status.APPROVED
        line = _new_line(editing, values, approved=approved)
        decided = approved or _decide_edited(editing, line, edit)
        revision = _revision(editing, line, Kind.ADD, {}, self_approved=decided)
        _finish(editing, revision, "add_line_as_reviewer")
        return revision

    return execute_once(
        actor_id=principal.user_id,
        key=idempotency_key,
        operation="request_line.add_as_reviewer",
        payload={"request_id": request_id, **edit.payload()},
        execute=execute,
        replay=_replay,
    )


@transactional
def remove_line_as_reviewer(
    principal: Principal,
    line_id: UUID,
    *,
    reason: str,
    seen_version: int | None = None,
    idempotency_key: str | None = None,
) -> MaterialRequestLineRevision:
    """Reject a line of a submitted request, or close a line of an approved one short; it
    stays on the request, never deleted (Q36)."""
    edit = LineEdit(reason=reason, seen_version=seen_version)
    reason = _reason(reason)
    locks = LockOrder()
    request = _line_request(line_id)
    actor = _authorize_edit(principal, request, locks, edit)

    def execute() -> MaterialRequestLineRevision:
        editing = _lock(principal, actor, request, locks, edit, reason)
        line = editing.line(line_id)
        before = revisions.values(line)
        if editing.request.status == Status.SUBMITTED:
            _require_may_decide(principal, actor, editing.request, EDIT)
            _reject(editing, line)
        else:
            _close_short(editing, line)
        revision = _revision(editing, line, Kind.REMOVE, before, self_approved=True)
        _finish(editing, revision, "remove_line_as_reviewer")
        return revision

    return execute_once(
        actor_id=principal.user_id,
        key=idempotency_key,
        operation="request_line.remove_as_reviewer",
        payload={"line_id": line_id, **edit.payload()},
        execute=execute,
        replay=_replay,
    )


def _replay(_type: str, pk: UUID) -> MaterialRequestLineRevision:
    return MaterialRequestLineRevision.objects.get(pk=pk)


# --- authorization, locks and the shared steps ----------------------------------------


def _check_fields(changes: Mapping[str, Any]) -> None:
    unknown = sorted(set(changes) - LINE_FIELDS)
    if unknown:
        raise ValueError(f"Request line fields that cannot be changed here: {unknown}")


def _authorize_edit(
    principal: Principal, request: MaterialRequest, locks: LockOrder, edit: LineEdit
) -> User:
    """``request.edit`` on the request; a decision in the same call also ``request.review``."""
    actor = _authorize(principal, EDIT, request, locks)
    if edit.decision or edit.approved_qty is not None:
        _authorize(principal, REVIEW, request, locks)
    return actor


def _lock(
    principal: Principal,
    actor: User,
    request: MaterialRequest,
    locks: LockOrder,
    edit: LineEdit,
    reason: str,
) -> _Editing:
    """Lock the component (L2-L5), check the state, the version and the decision fields,
    and find the as-submitted copy the edit revises (recorded now for a request submitted
    before copies existed)."""
    locked, lines, component = _lock_component(locks, request)
    _require_state(locked, Status.SUBMITTED, Status.APPROVED)
    _require_version(locked, edit.seen_version)
    if locked.status == Status.APPROVED:
        if edit.decision or edit.approved_qty is not None:
            raise ValidationError(
                _("An edit of an approved request is approved with it."), code="approved_request"
            )
        _require_may_decide(principal, actor, locked, EDIT)
    elif edit.decision or edit.approved_qty is not None:
        if edit.decision not in DECISIONS:
            raise ValidationError(
                {"decision": ValidationError(_("Approve or reject the line."), code="invalid")}
            )
        _require_may_decide(principal, actor, locked)
    baseline = locked.baselines.order_by("-sequence").first() or _record_baseline(
        locked, locked.requested_by_id, backfilled=True
    )
    return _Editing(principal, actor, locked, lines, component, baseline, reason, db_now())


def _new_line(editing: _Editing, values: dict[str, Any], *, approved: bool) -> MaterialRequestLine:
    """A line after the last one; approved by the editor at its quantity if ``approved``."""
    line = MaterialRequestLine(
        request=editing.request,
        project_id=editing.request.project_id,
        line_no=max(line.line_no for line in editing.lines) + 1,
        **values,
    )
    if approved:
        _approve(editing, line, values["requested_qty"])
    _save_line(line)
    editing.lines.append(line)
    return line


def _approve(editing: _Editing, line: MaterialRequestLine, quantity: Decimal) -> None:
    line.decision, line.approved_qty = Decision.APPROVED, quantity
    line.decided_by, line.decided_at = editing.actor, editing.now


def _decide_edited(editing: _Editing, line: MaterialRequestLine, edit: LineEdit) -> bool:
    """The editor's decision on the edited line of a submitted request, if any."""
    if not edit.decision:
        return False
    decision = LineDecision(line.pk, edit.decision, edit.approved_qty, editing.reason)
    apply_changes(line, _decision_values(decision, line), DECISION_FIELDS)
    line.decided_by, line.decided_at = editing.actor, editing.now
    line.save()
    return True


def _clear_decision(line: MaterialRequestLine) -> None:
    line.decision, line.approved_qty = Decision.PENDING, None
    line.decided_by, line.decided_at, line.decision_note = None, None, ""


def _revision(
    editing: _Editing,
    line: MaterialRequestLine,
    kind: str,
    before: dict[str, Any],
    *,
    self_approved: bool,
    replacement: MaterialRequestLine | None = None,
) -> MaterialRequestLineRevision:
    """Record the edit: ``after`` is the line now (the new line of a replacement)."""
    return MaterialRequestLineRevision.objects.create(
        request=editing.request,
        baseline=editing.baseline,
        line_id=line.pk,
        line_no=line.line_no,
        kind=kind,
        editor=editing.actor,
        reason=editing.reason,
        before=before,
        after=revisions.values(replacement or line),
        self_approved=self_approved,
        replacement_line_id=None if replacement is None else replacement.pk,
    )


def _finish(editing: _Editing, revision: MaterialRequestLineRevision, cause: str) -> None:
    """Audit the edit, finalise a submitted request whose lines are all decided, close or
    reopen an approved one automatically, reconcile the component and tell the requester."""
    request, principal = editing.request, editing.principal
    marker = _self_decided(editing.actor, request) if revision.self_approved else {}
    line = editing.line(revision.line_id)
    _record_line(
        principal,
        ACTIONS[Kind(revision.kind)],
        request,
        line,
        revision_id=revision.pk,
        kind=revision.kind,
        reason=revision.reason,
        before=revision.before,
        after=revision.after,
        self_approved=revision.self_approved,
        **marker,
    )
    if request.status == Status.SUBMITTED:
        _finalise(principal, request, marker, editing.now)
    else:
        _settle_closure(principal, request, editing.lines)
    editing.component.reconcile(principal, cause)
    events.publish(
        "request.edited",
        request_id=request.pk,
        revision_id=revision.pk,
        editor_id=editing.actor.pk,
    )


# --- submitted requests ---------------------------------------------------------------


def _content(
    line: MaterialRequestLine, changes: Mapping[str, Any]
) -> tuple[dict[str, Any], UnitOfMeasure]:
    """The line's material, description and unit after ``changes``, validated, with the
    unit. A catalog line's unit follows its material unless the edit names one."""
    merged = {name: getattr(line, name) for name in CONTENT_FIELDS} | {
        name: value for name, value in changes.items() if name in CONTENT_FIELDS
    }
    if merged["material_id"] is not None and "unit_id" not in changes:
        merged["unit_id"] = None
    return _line_content(
        **merged, new_material=merged["material_id"] not in {None, line.material_id}
    )


def _change_submitted(
    editing: _Editing, line: MaterialRequestLine, edit: LineEdit
) -> MaterialRequestLineRevision:
    """Change the line and clear its decision; the editor may decide it again at once."""
    before = revisions.values(line)
    content, unit = _content(line, edit.changes)
    quantity = edit.changes.get("requested_qty", line.requested_qty)
    check_quantity(quantity, unit, "requested_qty")
    if not apply_changes(line, {**content, "requested_qty": quantity}, LINE_FIELDS):
        raise _unchanged()
    _clear_decision(line)
    _save_line(line)
    decided = _decide_edited(editing, line, edit)
    return _revision(editing, line, Kind.CHANGE, before, self_approved=decided)


def _reject(editing: _Editing, line: MaterialRequestLine) -> None:
    if line.decision == Decision.REJECTED:
        raise ValidationError(_("This line is already rejected."), code="already_removed")
    line.decision, line.approved_qty, line.decision_note = (
        Decision.REJECTED,
        None,
        editing.reason,
    )
    line.decided_by, line.decided_at = editing.actor, editing.now
    line.save()


def _unchanged() -> ValidationError:
    return ValidationError(_("Change something before saving."), code="unchanged")


# --- approved requests ----------------------------------------------------------------


def _material_changed(line: MaterialRequestLine, content: dict[str, Any]) -> bool:
    """Another material or unit, or another description of a non-catalog line."""
    return (
        content["material_id"] != line.material_id
        or content["unit_id"] != line.unit_id
        or (content["material_id"] is None and content["free_text"] != line.free_text)
    )


def _change_approved(
    editing: _Editing, line: MaterialRequestLine, edit: LineEdit
) -> MaterialRequestLineRevision:
    """Change a live line in place, or replace it (see the module note)."""
    _require_live(line)
    content, unit = _content(line, edit.changes)
    (entry,) = ledger.line_ledgers([line])
    material_changed = _material_changed(line, content)
    if material_changed and not _in_place(editing, entry):
        return _replace(editing, entry, (content, unit), edit)
    before = revisions.values(line)
    values = dict(content)
    if "requested_qty" in edit.changes:
        quantity = edit.changes["requested_qty"]
        check_quantity(quantity, unit, "requested_qty")
        _require_floor(entry, quantity)
        values |= {"requested_qty": quantity, "approved_qty": quantity}
    else:  # the quantities stay: they must suit a new unit
        check_quantity(line.requested_qty, unit, "requested_qty")
        check_quantity(line.approved_qty, unit, "requested_qty")
    if material_changed:
        values["sourcing"] = Sourcing.UNDECIDED
    if not apply_changes(line, values, LINE_FIELDS | {"approved_qty", "sourcing"}):
        raise _unchanged()
    line.decided_by, line.decided_at = editing.actor, editing.now
    _save_line(line)
    return _revision(editing, line, Kind.CHANGE, before, self_approved=True)


def _in_place(editing: _Editing, entry: ledger.LineLedger) -> bool:
    """Nothing allocated (on any order but a cancelled one), received or delivered."""
    line = entry.line
    return (
        not editing.component.allocated_lines([line.pk])
        and entry.flows.received == 0
        and entry.flows.delivered == 0
    )


def _require_floor(entry: ledger.LineLedger, quantity: Decimal) -> None:
    if quantity < entry.floor:
        raise ValidationError(
            {
                "requested_qty": ValidationError(
                    _("%(floor)s are already delivered, received or on order."),
                    code="below_commitment",
                    params={"floor": revisions.quantity(entry.floor)},
                )
            }
        )


def _replace(
    editing: _Editing,
    entry: ledger.LineLedger,
    content: tuple[dict[str, Any], UnitOfMeasure],
    edit: LineEdit,
) -> MaterialRequestLineRevision:
    """Close the line short and add an approved line with the new material (Q36)."""
    line, (values, unit) = entry.line, content
    if entry.fully_delivered:
        raise ValidationError(
            _("This line is fully delivered: add a line instead."), code="fully_delivered"
        )
    quantity = _replacement_quantity(entry, unit, edit.changes)
    _require_open_project(_project(editing.request.project_id))
    before = revisions.values(line)
    _close_line(line, editing.actor, Closure.LINE, editing.reason, editing.now)
    new = _new_line(editing, {**values, "requested_qty": quantity}, approved=True)
    editing.component.trim_request_lines(
        editing.principal, [line.pk], reason=commitments.LINE_CLOSED, cause="edit_line"
    )
    return _revision(editing, line, Kind.REPLACE, before, self_approved=True, replacement=new)


def _replacement_quantity(
    entry: ledger.LineLedger, unit: UnitOfMeasure, changes: Mapping[str, Any]
) -> Decimal:
    """The edit's quantity, or what the replaced line still lacks, A - max(D, R), in the
    same unit."""
    if "requested_qty" in changes:
        quantity = changes["requested_qty"]
    elif unit.pk == entry.line.unit_id:
        quantity = entry.approved - max(entry.flows.delivered, entry.flows.received)
    else:
        quantity = None
    if not quantity:
        raise ValidationError(
            {
                "requested_qty": ValidationError(
                    _("Give the quantity of the new material."), code="required"
                )
            }
        )
    check_quantity(quantity, unit, "requested_qty")
    return Decimal(quantity)


def _close_short(editing: _Editing, line: MaterialRequestLine) -> None:
    """Remove a live line of an approved request: close it short and trim its allocations."""
    _require_live(line)
    (entry,) = ledger.line_ledgers([line])
    if entry.fully_delivered:
        raise ValidationError(
            _("This line is fully delivered: nothing is short."), code="fully_delivered"
        )
    _close_line(line, editing.actor, Closure.LINE, editing.reason, editing.now)
    editing.component.trim_request_lines(
        editing.principal,
        [line.pk],
        reason=commitments.LINE_CLOSED,
        cause="remove_line_as_reviewer",
    )
