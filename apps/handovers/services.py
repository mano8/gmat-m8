"""Handing material over from a warehouse, and voiding it (plan §2.3, §4.5, §4.6, §4.9).

Only ``FROM_WAREHOUSE`` handovers are posted and voided here; a ``DIRECT_RECEIPT`` one is
created and voided only with its goods receipt (``direct``, I8). Each service runs in one
transaction with ``lock_timeout`` through the shared retry wrapper, in the order of
CLAUDE.md "Writes":

1. ``authorize_in_txn`` in full: ``handover.post`` on the requests whose lines are handed
   over, ``handover.void`` on the handover; ``handover.view`` is the read capability (404
   outside it, 403 without the right);
2. the idempotency record of a posting (L1);
3. the allocation component of those requests (``purchasing.components``): every request
   header (L2) and line (L3), then every order header (L4) and line (L5) connected to
   them, discovered, locked and verified, because D is a reconciliation input; a void then
   locks the handover (L7);
4. validation under those locks: the project (S9), the source (C7), the branch (C8), each
   line (open, I5: never more than its outstanding A - D), the receiver;
5. the number last (L9), then the rows;
6. the automatic close or reopen of the requests (§4.9), then the reconciliation of the
   component: D rising above R raises the commitment max(D, R) + P + Q, so the pending
   above what is still needed is cut (audited trims, flagged as *unallocated on order*);
7. audit and events: ``handover.posted``, ``handover.voided`` (§4.10).

**Who posts** (plan §3.4, S5, C8): a handover is a document of its branch, which is the
procurement branch of every request it serves. Authorizing ``handover.post`` on those
requests is therefore authorizing it on the handover-to-be: a membership's branch scope
covers the requests exactly when it covers the handover (S6).

**Warehouse stock is not tracked in phase 1** (§2.3, point 3): what the warehouse received
and has not handed over yet is shown as a warning (``selectors.stock_warnings``), never a
limit. The hard limit is the line's approved quantity (I5).
"""

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.access.authorization import authorize_in_txn
from apps.access.context import AccessContext
from apps.access.principals import Principal
from apps.access.roles import Capability
from apps.access.scopes import object_in_scope
from apps.accounts.models import User
from apps.catalog.quantities import check_quantity
from apps.core.db import db_now
from apps.core.idempotency import execute_once
from apps.core.locking import LockLevel, LockOrder, transactional
from apps.handovers.common import assign_number, record_handover
from apps.handovers.models import Handover, HandoverLine
from apps.handovers.transitions import transition
from apps.locations.models import Location
from apps.material_requests import ledger as request_ledger
from apps.material_requests.models import MaterialRequest, MaterialRequestLine
from apps.material_requests.services.common import _settle_closure
from apps.organizations.models import Branch
from apps.projects.models import Project
from apps.purchasing.components import Component

VIEW, POST, VOID = Capability.HANDOVER_VIEW, Capability.HANDOVER_POST, Capability.HANDOVER_VOID
Kind, Status = Handover.Kind, Handover.Status
ZERO = Decimal("0")
NAME_LENGTH = 200  # Handover.received_by_name


@dataclass(frozen=True)
class HandoverLineInput:
    """What is handed over of one request line."""

    request_line_id: UUID
    qty: Decimal

    def payload(self) -> dict[str, Any]:
        return {"request_line_id": self.request_line_id, "qty": self.qty}


@dataclass(frozen=True)
class Receiver:
    """Who takes the material: a user of the project, or a name typed in (one of them)."""

    user_id: UUID | None = None
    name: str = ""

    def payload(self) -> dict[str, Any]:
        return {"user_id": self.user_id, "name": self.name.strip()}


# --- rules ----------------------------------------------------------------------------


def _reason(text: str) -> str:
    text = text.strip()
    if not text:
        raise ValidationError({"reason": ValidationError(_("Give a reason."), code="required")})
    return text


def _receiver_error(message: Any, code: str, **params: Any) -> ValidationError:
    return ValidationError(
        {"received_by": ValidationError(message, code=code, params=params or None)}
    )


def _check_receiver_shape(receiver: Receiver) -> None:
    name = receiver.name.strip()
    if receiver.user_id is not None and name:
        raise _receiver_error(_("Choose a person or type a name, not both."), "receiver_both")
    if receiver.user_id is None and not name:
        raise _receiver_error(_("Say who takes the material."), "receiver_required")
    if len(name) > NAME_LENGTH:
        raise _receiver_error(
            _("A name has at most %(limit)s characters."),
            "receiver_name_length",
            limit=NAME_LENGTH,
        )


def _receiver(receiver: Receiver, project: Project) -> User | None:
    """The receiving user: an active account that works on the project (``project.view``)."""
    if receiver.user_id is None:
        return None
    user = User.objects.filter(
        pk=receiver.user_id, is_active=True, offboarded_at__isnull=True
    ).first()
    if user is None or not object_in_scope(
        project, AccessContext.for_user(user).scope_for(Capability.PROJECT_VIEW)
    ):
        raise _receiver_error(_("Choose a person who works on this project."), "receiver")
    return user


def _requests(project_id: UUID, lines: Sequence[HandoverLineInput]) -> list[MaterialRequest]:
    """The requests of these lines, all of the project (C3); in id order."""
    wanted = {item.request_line_id for item in lines}
    found = MaterialRequestLine.objects.filter(pk__in=wanted, project_id=project_id)
    if found.count() != len(wanted):
        raise ValidationError(_("A line is not of this project."), code="unknown")
    requests = MaterialRequest.objects.filter(pk__in=found.values("request_id"))
    return list(requests.order_by("pk"))


def _active(branch: Branch) -> bool:
    return branch.is_active and branch.company.is_active


def _check_project(project: Project) -> None:
    """S9: an ``ON_HOLD`` or ``CLOSED`` project, or one of an inactive branch, takes none."""
    if project.status != Project.Status.ACTIVE:
        raise ValidationError(_("This project accepts no handovers."), code="project_not_active")
    if not _active(project.branch):
        raise ValidationError(_("The project's branch is inactive."), code="inactive_branch")


def _branch(requests: Sequence[MaterialRequest]) -> Branch:
    """C8: the handover's branch is the procurement branch of all its requests."""
    branches = {request.procurement_branch_id for request in requests}
    if len(branches) != 1:
        raise ValidationError(
            _("These requests are handled by different branches; hand them over separately."),
            code="branches",
        )
    branch = Branch.objects.select_related("company").get(pk=branches.pop())
    if not _active(branch):
        raise ValidationError(_("The requests' branch is inactive."), code="inactive_branch")
    return branch


def _check_source(branch: Branch, location: Location) -> None:
    """C7, S9: an active warehouse of the handover's branch scope (it or a branch it manages)."""
    home = location.branch
    if not (
        location.kind == Location.Kind.WAREHOUSE
        and location.is_active
        and _active(home)
        and branch.pk in {home.pk, home.managed_by_id}
    ):
        raise ValidationError(
            {
                "source_location": ValidationError(
                    _("Material for these requests cannot be taken from here."), code="location"
                )
            }
        )


def _line_label(line: MaterialRequestLine) -> dict[str, Any]:
    return {"request": line.request.number or "", "line": line.line_no}


def _check_line(item: HandoverLineInput, line: MaterialRequestLine, delivered: Decimal) -> None:
    """C10 and I5: an open line, and never more than its outstanding A - D."""
    label = _line_label(line)
    if line.request.status != MaterialRequest.Status.APPROVED or not request_ledger.is_live(line):
        raise ValidationError(
            _("%(request)s, line %(line)s: nothing can be handed over on it."),
            code="not_open",
            params=label,
        )
    check_quantity(item.qty, line.unit, "qty")
    outstanding = (line.approved_qty or ZERO) - delivered
    if item.qty > outstanding:
        raise ValidationError(
            _("%(request)s, line %(line)s: only %(outstanding)s remain to be handed over."),
            code="over_delivery",
            params={**label, "outstanding": f"{outstanding.normalize():f}"},
        )


def _locked_lines(lines: Sequence[HandoverLineInput]) -> dict[UUID, MaterialRequestLine]:
    """The request lines, read again under the component's locks (L3)."""
    found = MaterialRequestLine.objects.select_related("request", "unit").filter(
        pk__in=[item.request_line_id for item in lines]
    )
    return {line.pk: line for line in found}


# --- writing --------------------------------------------------------------------------


def _write_lines(handover: Handover, lines: Sequence[HandoverLineInput]) -> list[HandoverLine]:
    return HandoverLine.objects.bulk_create(
        HandoverLine(
            handover=handover,
            project=handover.project,
            request_line_id=item.request_line_id,
            qty=item.qty,
        )
        for item in lines
    )


def _settle(
    principal: Principal, component: Component, request_ids: Collection[UUID], cause: str
) -> None:
    """Automatic close and reopen of the requests (§4.9), then reconcile the component."""
    for request in MaterialRequest.objects.filter(pk__in=list(request_ids)).order_by("pk"):
        _settle_closure(principal, request, list(request.lines.all()))
    component.reconcile(principal, cause)


@dataclass(frozen=True)
class _Post:
    """A validated handover, under the component's locks."""

    branch: Branch
    project: Project
    location: Location
    received_by: User | None


def _validate(
    project_id: UUID,
    source_location_id: UUID,
    requests: Sequence[MaterialRequest],
    lines: Sequence[HandoverLineInput],
    receiver: Receiver,
) -> _Post:
    """Every rule of a posting, read again after L0 and under the component's locks."""
    project = Project.objects.select_related("branch__company").get(pk=project_id)
    _check_project(project)
    locked = list(MaterialRequest.objects.filter(pk__in=[r.pk for r in requests]))
    branch = _branch(locked)
    location = Location.objects.select_related("branch__company").get(pk=source_location_id)
    _check_source(branch, location)
    rows = _locked_lines(lines)
    delivered = request_ledger.flows(list(rows))
    for item in lines:
        _check_line(item, rows[item.request_line_id], delivered[item.request_line_id].delivered)
    return _Post(branch, project, location, _receiver(receiver, project))


# --- services -------------------------------------------------------------------------


@transactional
def create_handover(
    principal: Principal,
    project_id: UUID,
    *,
    source_location_id: UUID,
    lines: Sequence[HandoverLineInput],
    receiver: Receiver,
    idempotency_key: str | None = None,
) -> Handover:
    """Hand material of a warehouse over to the project's open request lines.

    Each line takes at most its outstanding quantity (I5); what the warehouse holds is
    not checked (phase 1 tracks no stock). With an idempotency key, a repeated post (a
    double tap) returns the handover again.
    """
    if not lines:
        raise ValidationError(_("Enter at least one line."), code="empty")
    if len({item.request_line_id for item in lines}) != len(lines):
        raise ValueError("Each request line is handed over once per handover")
    _check_receiver_shape(receiver)
    locks = LockOrder()
    requests = _requests(project_id, lines)
    actor = authorize_in_txn(principal, POST, *requests, locks=locks, read=VIEW)

    def execute() -> Handover:
        component = Component.lock(locks, requests=[request.pk for request in requests])
        post = _validate(project_id, source_location_id, requests, lines, receiver)
        handover = Handover(
            kind=Kind.FROM_WAREHOUSE,
            branch=post.branch,
            source_location=post.location,
            project=post.project,
            handed_by=actor,
            received_by=post.received_by,
            received_by_name=receiver.name.strip(),
            handed_at=db_now(),
        )
        assign_number(handover, post.branch, locks)
        handover.save()
        rows = _write_lines(handover, lines)
        record_handover(
            principal,
            "handover.posted",
            handover,
            project_id=post.project.pk,
            source_location_id=post.location.pk,
            received_by_id=handover.received_by_id,
            received_by_name=handover.received_by_name,
            lines=[{"request_line_id": row.request_line_id, "qty": row.qty} for row in rows],
        )
        _settle(principal, component, [request.pk for request in requests], "handover.post")
        return handover

    return execute_once(
        actor_id=principal.user_id,
        key=idempotency_key,
        operation="handover.post",
        payload={
            "project_id": project_id,
            "source_location_id": source_location_id,
            "receiver": receiver.payload(),
            "lines": [item.payload() for item in lines],
        },
        execute=execute,
        replay=lambda _type, pk: Handover.objects.get(pk=pk),
    )


@transactional
def void_handover(principal: Principal, handover_id: UUID, *, reason: str) -> Handover:
    """Void a posted ``FROM_WAREHOUSE`` handover (a reason is required).

    Its lines stop counting as delivered; the requests reopen when eligible and the
    component is reconciled (§4.9). A ``DIRECT_RECEIPT`` handover is refused: only its
    receipt's void removes it (I8).
    """
    reason = _reason(reason)
    locks = LockOrder()
    handover = Handover.objects.get(pk=handover_id)
    actor = authorize_in_txn(principal, VOID, handover, locks=locks, read=VIEW)
    if handover.kind == Kind.DIRECT_RECEIPT:
        raise ValidationError(
            _("This material was received on site; void its goods receipt instead."),
            code="direct_receipt",
        )
    request_ids = set(
        HandoverLine.objects.filter(handover=handover).values_list(
            "request_line__request_id", flat=True
        )
    )
    component = Component.lock(locks, requests=request_ids)
    locks.lock(LockLevel.HANDOVERS, Handover, [handover.pk])
    locked = Handover.objects.get(pk=handover.pk)
    left = transition(locked, Status.VOIDED, "void")
    locked.voided_at, locked.voided_by, locked.void_reason = db_now(), actor, reason
    locked.save()
    record_handover(principal, "handover.voided", locked, from_status=left, reason=reason)
    _settle(principal, component, request_ids, "handover.void")
    return locked
