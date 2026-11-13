"""What happened to an order: its audit history, its supplier dispatches, a request's trims."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from django.db.models import F, Q, QuerySet
from django.utils.translation import gettext_lazy as _

from apps.access.context import AccessContext
from apps.access.exceptions import NotInScope
from apps.access.roles import Capability
from apps.accounts.people import recorded_name
from apps.audit.models import AuditEvent
from apps.material_requests import selectors as request_selectors
from apps.material_requests.models import MaterialRequest
from apps.notifications.models import DISPATCH_KINDS, EmailOutbox, Kind
from apps.notifications.models import Status as Dispatch
from apps.purchasing.components import TrimReason
from apps.purchasing.models import PurchaseOrder
from apps.purchasing.selectors.purchase_orders import MANAGE, VIEW

NEEDS_ATTENTION = (Dispatch.AMBIGUOUS, Dispatch.QUARANTINED)
NOTICE_PENDING = (Dispatch.QUEUED, Dispatch.SENDING, Dispatch.FAILED)


def dispatches_needing_attention(ctx: AccessContext) -> QuerySet[EmailOutbox]:
    """Supplier dispatches of the user's ``po.manage`` orders a human has to look at: left
    ``AMBIGUOUS`` or ``QUARANTINED``, and cancellation notices not yet sent (M7.2)."""
    return (
        EmailOutbox.objects.filter(
            Q(status__in=NEEDS_ATTENTION) | Q(kind=Kind.PO_CANCELLATION, status__in=NOTICE_PENDING),
            kind__in=DISPATCH_KINDS,
            source_po__in=PurchaseOrder.objects.visible_to(ctx, MANAGE),
        )
        .select_related("source_po")
        .order_by("created_at")
    )


# --- history: revisions, amendments and the reconciliation trims -----------------------------


@dataclass(frozen=True)
class HistoryEntry:
    occurred_at: datetime
    actor: str
    label: Any
    detail: str

    @property
    def when(self) -> datetime:
        """The time, as the ``timeline`` component names it."""
        return self.occurred_at

    @property
    def title(self) -> str:
        """What happened (the ``timeline`` component's headline)."""
        return str(self.label)


ACTION_LABELS = {
    "purchase_order.created": _("Draft created"),
    "purchase_order.updated": _("Draft edited"),
    "purchase_order.issued": _("Issued"),
    "purchase_order.amended": _("Amended"),
    "purchase_order.cancelled": _("Cancelled"),
    "purchase_order.discarded": _("Draft discarded"),
    "purchase_order.auto_closed": _("Closed: nothing left to receive"),
    "purchase_order.auto_reopened": _("Reopened"),
    "purchase_order_line.closed_short": _("Line closed short"),
    "purchase_allocation.trimmed": _("Allocation trimmed"),
    "purchase_order.dispatch_started": _("Sending to the supplier"),
    "purchase_order.dispatch_sent": _("Sent to the supplier"),
    "purchase_order.dispatch_failed": _("Sending to the supplier failed"),
    "purchase_order.dispatch_ambiguous": _("Delivery to the supplier uncertain"),
    "purchase_order.dispatch_confirmed": _("Delivery to the supplier confirmed"),
    "purchase_order.dispatch_resent": _("Sent to the supplier again"),
}
TRIM_LABELS = {
    TrimReason.EXCESS_COMMITMENT: _("the request line was already committed elsewhere"),
    TrimReason.REQUEST_LINE_CLOSED: _("the request line was closed short"),
    TrimReason.REQUEST_CLOSED: _("the request was closed"),
    TrimReason.ORDER_LINE_CLOSED: _("the order line was closed short"),
    TrimReason.ORDER_CANCELLED: _("the order was cancelled"),
    TrimReason.DRAFT_DISCARDED: _("the draft was discarded"),
    TrimReason.ORDERED_QTY_REDUCED: _("the ordered quantity was lowered"),
    TrimReason.UNALLOCATED: _("unallocated"),
}


def _quantity(value: Any) -> str:
    return f"{Decimal(str(value)).normalize():f}"


def _amended(data: dict[str, Any]) -> list[str]:
    return [f"#{data.get('revision')}", str(data.get("reason", ""))]


def _trimmed(data: dict[str, Any]) -> list[str]:
    reason = TrimReason(data["reason"]) if data.get("reason") in TrimReason else None
    return [
        f"{data.get('request')} / {data.get('order_line')}: "
        f"{_quantity(data.get('from'))} → {_quantity(data.get('to'))}",
        str(TRIM_LABELS.get(reason, "")) if reason else "",
    ]


def _closed_short(data: dict[str, Any]) -> list[str]:
    return [f"#{data.get('line_no')}", str(data.get("reason", ""))]


def _dispatched(data: dict[str, Any]) -> list[str]:
    notice = data.get("kind") == Kind.PO_CANCELLATION
    return [
        str(_("cancellation notice")) if notice else f"#{data.get('revision')}",
        str(data.get("error") or data.get("note") or ""),
    ]


def _reason_only(data: dict[str, Any]) -> list[str]:
    return [str(data.get("reason", ""))]


DETAILS = {
    "purchase_order.amended": _amended,
    "purchase_allocation.trimmed": _trimmed,
    "purchase_order_line.closed_short": _closed_short,
}


def _detail(action: str, data: dict[str, Any]) -> str:
    """The reason or what changed in a few words; never the raw payload."""
    describe = (
        _dispatched
        if action.startswith("purchase_order.dispatch_")
        else DETAILS.get(action, _reason_only)
    )
    return " · ".join(part for part in describe(data) if part)


# --- supplier dispatches (M5.3) ---------------------------------------------------------


@dataclass(frozen=True)
class DispatchRow:
    """A supplier dispatch of an order; ``decidable`` ones wait for a human (plan §5)."""

    dispatch: EmailOutbox
    notice: bool  # the cancellation notice, not a revision of the order
    revision: int | None
    decidable: bool
    resendable: bool

    @property
    def confirmable(self) -> bool:
        return self.dispatch.status in (Dispatch.AMBIGUOUS, Dispatch.QUARANTINED)

    @property
    def actionable(self) -> bool:
        return self.resendable or self.confirmable


def _dispatches(ctx: AccessContext) -> QuerySet[EmailOutbox]:
    return EmailOutbox.objects.filter(
        kind__in=DISPATCH_KINDS, source_po__in=PurchaseOrder.objects.visible_to(ctx, VIEW)
    )


def dispatch(ctx: AccessContext, dispatch_id: UUID) -> EmailOutbox:
    found = _dispatches(ctx).filter(pk=dispatch_id).select_related("source_po").first()
    if found is None:
        raise NotInScope(VIEW)
    return found


def dispatches(ctx: AccessContext, of: PurchaseOrder) -> list[DispatchRow]:
    """The order's dispatches, newest first, with what a human may still do with each."""
    from apps.purchasing.dispatch import DECIDABLE, refusal, revision_of

    rows = []
    for found in _dispatches(ctx).filter(source_po=of).order_by("-created_at"):
        decidable = found.status in DECIDABLE
        rows.append(
            DispatchRow(
                dispatch=found,
                notice=found.kind == Kind.PO_CANCELLATION,
                revision=revision_of(found),
                decidable=decidable,
                resendable=decidable and refusal(of, found) is None,
            )
        )
    return rows


def awaiting_decision(rows: Iterable[DispatchRow]) -> bool:
    return any(row.confirmable for row in rows)


def notice_awaited(of: PurchaseOrder, rows: list[DispatchRow]) -> bool:
    """Cancelled while a dispatch was being sent: the notice follows if that one reaches the
    supplier (it is listed once queued; nothing is owed if the attempt fails)."""
    return (
        of.cancellation_notice_required
        and not any(row.notice for row in rows)
        and any(row.dispatch.status == Dispatch.SENDING for row in rows)
    )


def can_see_history(ctx: AccessContext, of: PurchaseOrder) -> bool:
    """Who changed what and why: for who manages the order or audits it."""
    return ctx.can(MANAGE, of) or ctx.can(Capability.AUDIT_VIEW, of)


def history(ctx: AccessContext, of: PurchaseOrder) -> list[HistoryEntry]:
    """The order's audit events and its allocations' trims, oldest first (else nothing)."""
    if not can_see_history(ctx, of):
        return []
    events = (
        AuditEvent.objects.filter(
            Q(object_type=PurchaseOrder._meta.label_lower, object_id=of.pk)
            | Q(action="purchase_allocation.trimmed", data__order_id=str(of.pk))
        )
        .annotate(actor_name=F("actor__full_name"))
        .order_by("occurred_at", "id")
    )
    return [
        HistoryEntry(
            occurred_at=event.occurred_at,
            actor=recorded_name(event),  # ADR-0029
            label=ACTION_LABELS.get(event.action, event.action),
            detail=_detail(event.action, event.data),
        )
        for event in events
        if event.action in ACTION_LABELS
    ]


# --- a request's trims, for its ledger page ------------------------------------------------


@dataclass(frozen=True)
class RequestTrim:
    """A lowered allocation as the request side sees it: no order, no supplier."""

    occurred_at: datetime
    actor: str
    line_no: int
    before: Decimal
    after: Decimal
    reason: Any


def request_trims(ctx: AccessContext, of: MaterialRequest) -> dict[int, list[RequestTrim]]:
    """The trims of the request's allocations by request line number, oldest first.

    Closures and reconciliation cuts (§4.5) explain why a line holds less than it did; they
    reach who follows the request up (``request_selectors.can_see_history``), else nothing.
    """
    if not request_selectors.can_see_history(ctx, of):
        return {}
    events = (
        AuditEvent.objects.filter(action="purchase_allocation.trimmed", data__request_id=str(of.pk))
        .annotate(actor_name=F("actor__full_name"))
        .order_by("occurred_at", "id")
    )
    grouped: dict[int, list[RequestTrim]] = {}
    for event in events:
        data = event.data
        reason = TrimReason(data["reason"]) if data.get("reason") in TrimReason else None
        grouped.setdefault(int(data["request_line"]), []).append(
            RequestTrim(
                occurred_at=event.occurred_at,
                actor=recorded_name(event),  # ADR-0029
                line_no=int(data["request_line"]),
                before=Decimal(str(data["from"])),
                after=Decimal(str(data["to"])),
                reason=TRIM_LABELS.get(reason, "") if reason else "",
            )
        )
    return grouped
