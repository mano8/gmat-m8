"""What happened to a request: its audit history, the as-submitted copy, the revisions (M7b.3)."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from django.utils.translation import gettext_lazy as _

from apps.access.context import AccessContext
from apps.audit.models import AuditEvent
from apps.material_requests import revisions
from apps.material_requests.models import (
    MaterialRequest,
    MaterialRequestBaseline,
    MaterialRequestBaselineLine,
    MaterialRequestLine,
    MaterialRequestLineRevision,
)
from apps.material_requests.selectors.requester import VIEW
from apps.material_requests.selectors.reviewer import can_see_history

ACTION_LABELS = {
    "request.created": _("Draft created"),
    "request.updated": _("Request edited"),
    "request.submitted": _("Submitted"),
    "request.withdrawn": _("Withdrawn"),
    "request.discarded": _("Draft discarded"),
    "request.cancelled": _("Cancelled"),
    "request.returned": _("Returned for changes"),
    "request.approved": _("Approved"),
    "request.rejected": _("Rejected"),
    "request.rescheduled": _("Schedule changed"),
    "request.reviewer_assigned": _("Sent to a reviewer"),
    "request.closed": _("Closed"),
    "request.reopened": _("Reopened"),
    "request.auto_closed": _("Closed automatically"),
    "request.auto_reopened": _("Reopened automatically"),
    "request_line.added": _("Line added"),
    "request_line.updated": _("Line edited"),
    "request_line.removed": _("Line removed"),
    "request_line.decided": _("Line decided"),
    "request_line.reduced": _("Approved quantity reduced"),
    "request_line.sourcing_set": _("Sourcing set"),
    "request_line.resolved": _("Linked to the catalog"),
    "request_line.closed_short": _("Line closed short"),
    "request_line.reopened": _("Line reopened"),
    "request_line.edited": _("Line changed by a reviewer"),
    "request_line.added_by_reviewer": _("Line added by a reviewer"),
    "request_line.removed_by_reviewer": _("Line removed by a reviewer"),
    "request_line.replaced": _("Line replaced by a reviewer"),
    "attachment.added": _("File attached"),
    "attachment.removed": _("File removed"),
}


@dataclass(frozen=True)
class HistoryEntry:
    occurred_at: datetime
    actor: str
    action: str
    label: Any
    line_no: int | None
    detail: str


def _quantity(value: Any) -> str:
    return f"{Decimal(str(value)).normalize():f}"


def _to(changes: dict[str, Any], name: str) -> Any:
    """The new value of a changed field, if it changed."""
    return (changes.get(name) or {}).get("to")


def _join(*parts: str) -> str:
    return " · ".join(part for part in parts if part)


def _decided_detail(data: dict[str, Any], changes: dict[str, Any]) -> str:
    decision, quantity = _to(changes, "decision"), _to(changes, "approved_qty")
    return _join(
        str(MaterialRequestLine.Decision(decision).label) if decision else "",
        _quantity(quantity) if quantity is not None else "",
        _to(changes, "decision_note") or "",
    )


def _reduced_detail(data: dict[str, Any], changes: dict[str, Any]) -> str:
    change = changes.get("approved_qty") or {}
    arrow = f"{_quantity(change['from'])} → {_quantity(change['to'])}" if change else ""
    return _join(arrow, data.get("reason", ""))


def _sourcing_detail(data: dict[str, Any], changes: dict[str, Any]) -> str:
    return str(_to(changes, "sourcing") or "")


def _rescheduled_detail(data: dict[str, Any], changes: dict[str, Any]) -> str:
    return _join(*(f"{name}: {change.get('to') or '—'}" for name, change in changes.items()))


def _attachment_detail(data: dict[str, Any], changes: dict[str, Any]) -> str:
    return _join(data.get("file_name", ""), data.get("reason", ""))


_DETAILS = {
    "attachment.added": _attachment_detail,
    "attachment.removed": _attachment_detail,
    "request_line.decided": _decided_detail,
    "request_line.reduced": _reduced_detail,
    "request_line.sourcing_set": _sourcing_detail,
    "request.rescheduled": _rescheduled_detail,
}


def _detail(action: str, data: dict[str, Any]) -> str:
    """The reason or note of an event, or what it changed; never the raw payload."""
    handler = _DETAILS.get(action)
    if handler is not None:
        return handler(data, data.get("changes") or {})
    return str(data.get("reason") or data.get("note") or "")


def history(ctx: AccessContext, of: MaterialRequest) -> list[HistoryEntry]:
    """The request's audit events, oldest first, for who may see them (else nothing)."""
    if not can_see_history(ctx, of):
        return []
    events = AuditEvent.objects.filter(
        object_type=MaterialRequest._meta.label_lower, object_id=of.pk
    ).order_by("occurred_at", "id")
    return [
        HistoryEntry(
            occurred_at=event.occurred_at,
            actor=event.actor_label,
            action=event.action,
            label=ACTION_LABELS.get(event.action, event.action),
            line_no=event.data.get("line_no"),
            detail=_detail(event.action, event.data),
        )
        for event in events
    ]


# --- reviewer edits: the as-submitted copy and the revisions (M7b.3, Q34) ---------------


@dataclass(frozen=True)
class LineHistory:
    """A line of the request, as its requester submitted it, and the reviewers' edits.

    ``submitted`` is ``None`` for a line a reviewer added (or that replaced another);
    ``revisions`` are oldest first and include the one that created the line and the one
    that replaced it. Nothing beyond what the line itself shows: material, description,
    unit, quantities, decision, who edited it, when and why.
    """

    line: MaterialRequestLine
    submitted: MaterialRequestBaselineLine | None
    revisions: tuple[MaterialRequestLineRevision, ...]

    @property
    def edited(self) -> bool:
        return bool(self.revisions)


def version(of: MaterialRequest) -> int:
    """What the page sends back as ``seen_version`` with a decision or an edit."""
    return revisions.version(of.pk)


def line_history(ctx: AccessContext, of: MaterialRequest) -> list[LineHistory]:
    """Every line of the request (removed ones included: they are rejected or closed,
    never deleted) against the copy of its latest submit, with the edits made since.

    Empty for who may not view the request. A request submitted before copies existed has
    one only once a reviewer edits it (``backfilled``).
    """
    if not ctx.can(VIEW, of):
        return []
    baseline = (
        MaterialRequestBaseline.objects.visible_to(ctx, VIEW)
        .filter(request=of)
        .order_by("-sequence")
        .first()
    )
    copies: dict[UUID, MaterialRequestBaselineLine] = {}
    edits: list[MaterialRequestLineRevision] = []
    if baseline is not None:
        copies = {copy.line_id: copy for copy in baseline.lines.select_related("material", "unit")}
        edits = list(
            MaterialRequestLineRevision.objects.visible_to(ctx, VIEW)
            .filter(baseline=baseline)
            .select_related("editor")
            .order_by("created_at", "id")
        )
    rows = (
        MaterialRequestLine.objects.visible_to(ctx, VIEW)
        .filter(request=of)
        .select_related("material", "unit")
        .order_by("line_no")
    )
    return [
        LineHistory(
            line=row,
            submitted=copies.get(row.pk),
            revisions=tuple(
                edit for edit in edits if row.pk in {edit.line_id, edit.replacement_line_id}
            ),
        )
        for row in rows
    ]
