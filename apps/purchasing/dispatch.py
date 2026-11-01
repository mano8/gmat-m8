"""Supplier dispatches of purchase orders (plan §5, §4.7 L4b; M5.3, Q7).

An order's ``PO_ORDER`` / ``PO_CANCELLATION`` outbox rows are locked after the order (L4 →
L4b) by every path, so a cancellation, an amendment and a worker claim never deadlock
(F55). Their content is frozen from the order's snapshots when they are queued.

- ``queue_order``: at issue and at each amendment, with ``PO_DISPATCH`` on (ADR-0010 keeps
  the MVP on the printable page). The still-``QUEUED`` ``PO_ORDER`` rows of older revisions
  become ``SUPERSEDED``; a row already ``SENDING``, ``SENT`` or ``AMBIGUOUS`` is never
  changed. The new row has ``source_ref = PO:{id}:rev{n}`` and a ``Message-ID`` stable per
  revision, so the supplier can deduplicate.
- ``cancel_dispatches``: every ``QUEUED`` ``PO_ORDER`` becomes ``CANCELLED`` (nothing unsent
  goes out); if the supplier may have one (``SENT``, ``AMBIGUOUS``, or queued again by a
  human after an ambiguous attempt) the ``PO_CANCELLATION`` notice is queued at once; if one
  is being sent, ``cancellation_notice_required`` is set, and the worker's
  outcome step (``OrderDispatches.after_outcome``) queues the notice when it ends ``SENT`` or
  ``AMBIGUOUS``. The notice is unique per order and goes out whatever ``PO_DISPATCH`` says.
- ``OrderDispatches``: the worker's ``DispatchSource`` (registered in ``apps.py``): the order
  ``FOR SHARE`` at L4, eligibility at the claim (a ``PO_ORDER`` of the current revision of an
  order that is not cancelled, so an amendment that closes the order still goes out; a
  ``PO_CANCELLATION`` while its order is cancelled), the obligation, and an audit event on
  the order for every attempt.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any, cast
from uuid import UUID

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q, QuerySet
from django.template.loader import render_to_string
from django.utils import timezone, translation
from django.utils.translation import gettext as _

from apps.access.principals import SystemPrincipal
from apps.audit.recorder import record
from apps.core import events
from apps.core.locking import LockLevel, LockMode, LockOrder
from apps.notifications.models import (
    DISPATCH_KINDS,
    EmailAttempt,
    EmailOutbox,
    Kind,
    Outcome,
    Status,
)
from apps.notifications.outbox import queue_dispatch, sender_domain
from apps.purchasing.ledger import book_for_order_lines
from apps.purchasing.models import PurchaseOrder, PurchaseOrderLine
from apps.purchasing.selectors import final_qty, line_total

# The supplier may have these: the cancellation notice is owed now.
DELIVERED = frozenset({Status.SENT, Status.AMBIGUOUS})
# A human decides on these (never resent automatically after AMBIGUOUS, plan §5).
DECIDABLE = frozenset({Status.AMBIGUOUS, Status.FAILED})

SOURCE_REF = re.compile(r"PO:(?P<po>[0-9a-f-]{36}):rev(?P<revision>\d+)")


class Queued(StrEnum):
    """What ``queue_order`` did, for the audit trail of the issue or amendment."""

    QUEUED = "queued"
    OFF = "off"  # PO_DISPATCH is off: the printable page only (Q7)
    NO_ADDRESS = "no_address"  # the supplier had no email address when the order was issued


def source_ref(order: PurchaseOrder) -> str:
    return f"PO:{order.pk}:rev{order.revision}"


def revision_of(message: EmailOutbox) -> int | None:
    found = SOURCE_REF.fullmatch(message.source_ref)
    return int(found["revision"]) if found else None


def _message_id(order: PurchaseOrder, suffix: str) -> str:
    domain = sender_domain(settings.DEFAULT_FROM_EMAIL) or "localhost"
    return f"<po-{order.pk}-{suffix}@{domain}>"


def lock_dispatches(po_id: UUID) -> Callable[[LockOrder], list[UUID]]:
    """L4b: the order's dispatch rows, after its L4 lock (none can be added while held)."""

    def lock(locks: LockOrder) -> list[UUID]:
        rows = EmailOutbox.objects.filter(source_po_id=po_id, kind__in=DISPATCH_KINDS)
        return locks.lock(LockLevel.PO_OUTBOX, EmailOutbox, rows.values_list("pk", flat=True))

    return lock


# --- content ----------------------------------------------------------------------------


@dataclass(frozen=True)
class _Line:
    line: PurchaseOrderLine
    reference: str
    name: str
    quantity: Decimal  # what it orders in the end: the received quantity once closed short
    total: Decimal | None


def _line(line: PurchaseOrderLine, received: Decimal) -> _Line:
    quantity = final_qty(line, received)
    return _Line(
        line=line,
        reference=line.material_snapshot.get("reference", ""),
        name=line.material_snapshot.get("name", ""),
        quantity=quantity,
        total=line_total(line, quantity),
    )


def _context(order: PurchaseOrder) -> dict[str, Any]:
    """The order as issued (its snapshots); prices as recorded, if any (the supplier's own)."""
    lines = list(order.lines.select_related("unit").order_by("line_no"))
    book = book_for_order_lines([line.pk for line in lines])
    rows = [_line(line, book.order_line_totals(line.pk).received) for line in lines]
    totals = [row.total for row in rows if row.total is not None]
    return {
        "order": order,
        "lines": rows,
        "prices": any(row.line.unit_price is not None for row in rows),
        "order_total": sum(totals, Decimal(0)) if totals else None,
    }


def _render(order: PurchaseOrder, template: str, kind: str) -> dict[str, str]:
    """Subject, text and HTML bodies in the installation's language, frozen at queueing."""
    with translation.override(settings.LANGUAGE_CODE):
        context = _context(order)
        return {
            "subject": subject_for(order, kind),
            "body": render_to_string(f"purchasing/email/{template}.txt", context),
            "html_body": render_to_string(f"purchasing/email/{template}.html", context),
        }


def subject_for(order: PurchaseOrder, kind: str) -> str:
    if kind == Kind.PO_CANCELLATION:
        return _("Cancellation of purchase order %(number)s") % {"number": order.number}
    if order.revision:
        return _("Purchase order %(number)s, revision %(revision)s") % {
            "number": order.number,
            "revision": order.revision,
        }
    return _("Purchase order %(number)s") % {"number": order.number}


# --- queueing ---------------------------------------------------------------------------


def queue_order(order: PurchaseOrder) -> Queued:
    """Queue this revision of an issued order; supersede older queued revisions.

    The caller holds the order ``FOR UPDATE`` (L4) and its dispatch rows (L4b).
    """
    if not settings.PO_DISPATCH:
        return Queued.OFF
    address = (order.supplier_snapshot or {}).get("email", "")
    superseded = (
        EmailOutbox.objects.filter(source_po=order, kind=Kind.PO_ORDER, status=Status.QUEUED)
        .exclude(source_ref=source_ref(order))
        .update(status=Status.SUPERSEDED, updated_at=timezone.now())
    )
    if superseded:
        events.publish("purchase_order.dispatch_superseded", po_id=order.pk, count=superseded)
    if not address:
        return Queued.NO_ADDRESS
    queue_dispatch(
        kind=Kind.PO_ORDER,
        po_id=order.pk,
        address=address,
        source_ref=source_ref(order),
        message_id=_message_id(order, f"rev{order.revision}"),
        **_render(order, "order", Kind.PO_ORDER),
    )
    return Queued.QUEUED


def _may_have_reached(order: PurchaseOrder) -> QuerySet[EmailOutbox]:
    """The order's ``PO_ORDER`` rows the supplier may have: sent or ambiguous, or queued
    again by a human after an ambiguous attempt."""
    ambiguous = EmailAttempt.objects.filter(outcome=Outcome.AMBIGUOUS).values("message_id")
    return EmailOutbox.objects.filter(source_po=order, kind=Kind.PO_ORDER).filter(
        Q(status__in=DELIVERED) | Q(pk__in=ambiguous)
    )


def queue_cancellation_notice(order: PurchaseOrder) -> EmailOutbox | None:
    """Queue the order's ``PO_CANCELLATION`` notice once, to the address its orders went to.

    Idempotent: unique per order (F53). Two outcome transactions of one order hold it only
    ``FOR SHARE``, so the loser of an insert race finds the notice there and does nothing.
    """
    notices = EmailOutbox.objects.filter(source_po=order, kind=Kind.PO_CANCELLATION)
    if notices.exists():
        return None
    sent = (
        _may_have_reached(order)
        .order_by("-created_at")
        .values_list("recipient_address", flat=True)
        .first()
    )
    if sent is None:
        return None
    try:
        with transaction.atomic():
            return queue_dispatch(
                kind=Kind.PO_CANCELLATION,
                po_id=order.pk,
                address=sent,
                source_ref=f"PO:{order.pk}:cancel",
                message_id=_message_id(order, "cancel"),
                **_render(order, "cancellation", Kind.PO_CANCELLATION),
            )
    except IntegrityError:
        if notices.exists():
            return None
        raise


@dataclass(frozen=True)
class Cancelled:
    """What the cancellation did with the order's dispatches."""

    cancelled: int
    notice_queued: bool


def cancel_dispatches(order: PurchaseOrder) -> Cancelled:
    """Cancel the order's unsent dispatches; owe the supplier a notice for the others.

    The caller holds the order ``FOR UPDATE`` (L4) and its dispatch rows (L4b), and saves
    the order.
    """
    orders = EmailOutbox.objects.filter(source_po=order, kind=Kind.PO_ORDER)
    reached = _may_have_reached(order).exists()  # before the queued ones are cancelled
    if reached or orders.filter(status=Status.SENDING).exists():
        order.cancellation_notice_required = True
    notice = queue_cancellation_notice(order) if reached else None
    cancelled = orders.filter(status=Status.QUEUED).update(
        status=Status.CANCELLED, updated_at=timezone.now()
    )
    return Cancelled(cancelled=cancelled, notice_queued=notice is not None)


# --- the worker's source ----------------------------------------------------------------


def _order_of(message: EmailOutbox) -> PurchaseOrder:
    return PurchaseOrder.objects.get(pk=cast(UUID, message.source_po_id))  # a dispatch has one


def refusal(order: PurchaseOrder, message: EmailOutbox) -> Status | None:
    """``None`` while ``message`` may be sent; else the terminal status it gets instead."""
    cancelled = order.status == PurchaseOrder.Status.CANCELLED
    if message.kind == Kind.PO_CANCELLATION:
        return None if cancelled else Status.CANCELLED
    if cancelled:
        return Status.CANCELLED
    if revision_of(message) != order.revision:
        return Status.SUPERSEDED
    return None


ATTEMPT_ACTIONS = {
    "": "purchase_order.dispatch_started",
    "SENT": "purchase_order.dispatch_sent",
    "FAILED": "purchase_order.dispatch_failed",
    "AMBIGUOUS": "purchase_order.dispatch_ambiguous",
}


class OrderDispatches:
    """The purchasing app's ``DispatchSource`` (``apps.notifications.dispatch``)."""

    def lock_order(self, locks: LockOrder, po_id: UUID) -> None:
        locks.lock(LockLevel.PO_HEADERS, PurchaseOrder, [po_id], mode=LockMode.SHARE)

    def refusal(self, message: EmailOutbox) -> Status | None:
        return refusal(_order_of(message), message)

    def after_outcome(self, message: EmailOutbox, locks: LockOrder) -> None:
        """The cancellation obligation (F53): the order was cancelled while this was sent."""
        if message.kind != Kind.PO_ORDER:
            return
        order = _order_of(message)
        if order.status == PurchaseOrder.Status.CANCELLED:
            queue_cancellation_notice(order)

    def record_attempt(
        self, principal: SystemPrincipal, message: EmailOutbox, attempt: EmailAttempt
    ) -> None:
        order = _order_of(message)
        record(
            principal.audit_identity,
            ATTEMPT_ACTIONS[attempt.outcome],
            obj=order,
            branch_id=order.branch_id,
            data={
                "number": order.number,
                "dispatch_id": message.pk,
                "kind": message.kind,
                "revision": revision_of(message),
                "attempt": attempt.number,
                "abandoned": attempt.abandoned,
                "error": attempt.error,
            },
        )


# The one instance ``apps.py`` registers (again, harmlessly, when ``ready`` runs again).
SOURCE = OrderDispatches()
