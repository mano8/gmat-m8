"""Reads of the notification screens (M7.1) and the delivery badges (M7c.1)."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from django.db.models import OuterRef, QuerySet, Subquery
from django.utils.functional import Promise
from django.utils.timezone import now
from django.utils.translation import gettext_lazy as _

from apps.access.context import AccessContext
from apps.access.roles import Capability
from apps.notifications.models import EmailOutbox, Kind, NotificationPreference, Status

# The security mail whose status a group administrator may see: the objects they manage
# (``eligibility_object_type`` of the invitation and of the email change).
INVITATION = "accounts.invitation"
EMAIL_CHANGE = "accounts.emailchangerequest"
DELIVERY_OBJECT_TYPES = frozenset({INVITATION, EMAIL_CHANGE})

# A message still queued this long after it was queued: the worker is paused, outbound mail
# is off or the worker is down. ``web`` does not know which (Q38); it tells the person to ask.
DELAYED_AFTER = timedelta(minutes=10)

IN_FLIGHT = frozenset({Status.QUEUED, Status.SENDING})

_TONES: dict[str, str] = {
    Status.QUEUED: "brand",
    Status.SENDING: "brand",
    Status.SENT: "ok",
    Status.FAILED: "danger",
    Status.AMBIGUOUS: "warn",
    Status.EXPIRED: "warn",
    Status.QUARANTINED: "danger",
}
_HINTS: dict[str, Promise] = {
    Status.QUEUED: _("Waiting for the mail worker."),
    Status.SENDING: _("Being handed to the mail server."),
    Status.FAILED: _("The mail server refused it. Nothing was delivered."),
    Status.AMBIGUOUS: _("The mail server did not answer: it may or may not have arrived."),
    Status.SUPERSEDED: _("Replaced by a newer message."),
    Status.CANCELLED: _("Cancelled before it was sent."),
    Status.SUPPRESSED: _("Not sent: it no longer applied when its turn came."),
    Status.EXPIRED: _("Not sent in time: its link expired."),
    Status.QUARANTINED: _("Held back by the system. Tell the operator."),
}
DELAYED_LABEL = _("Delayed")
DELAYED_HINT = _("Still not sent. Tell the operator.")


@dataclass(frozen=True)
class DeliveryStatus:
    """What a screen may show of one email: its state and when, never what it said."""

    state: str
    at: datetime  # when it was sent; else when it was queued
    delayed: bool

    @property
    def label(self) -> str:
        return str(DELAYED_LABEL if self.delayed else Status(self.state).label)

    @property
    def hint(self) -> str:
        return str(DELAYED_HINT if self.delayed else _HINTS.get(self.state, ""))

    @property
    def tone(self) -> str:
        return "warn" if self.delayed else _TONES.get(self.state, "")

    @property
    def polling(self) -> bool:
        """Whether the badge refreshes itself: only while the worker may still change it."""
        return self.state in IN_FLIGHT


def email_opt_out(ctx: AccessContext) -> bool:
    """Whether the signed-in user turned business email off."""
    if ctx.user_id is None:
        return False
    return NotificationPreference.objects.filter(user_id=ctx.user_id, email_opt_out=True).exists()


def with_delivery_state(rows: QuerySet[Any], object_type: str) -> QuerySet[Any]:
    """``rows`` (objects of ``object_type``, read through a scoped selector) annotated with
    ``delivery_state``: the status of the latest security email about each, ``None`` for none.
    Only the status is selected, as in :func:`delivery_status`."""
    latest = (
        EmailOutbox.objects.filter(
            kind=Kind.SECURITY,
            eligibility_object_type=object_type,
            eligibility_object_id=OuterRef("pk"),
        )
        .order_by("-created_at", "-pk")
        .values("status")[:1]
    )
    annotated: QuerySet[Any] = rows.annotate(delivery_state=Subquery(latest))
    return annotated


def delivery_status(
    ctx: AccessContext, object_type: str, object_ids: Iterable[UUID], *, source_ref: str = ""
) -> dict[UUID, DeliveryStatus]:
    """The latest security email about each object, in one query (Q37).

    The caller passes only objects it already read through its own scoped selector; a
    group administrator's ``org.manage`` is checked again here. ``source_ref`` picks one of
    several messages about the same object (the confirm and notice mails of an email
    change). Only the state, the times and the kind are selected: never the body, the sealed
    content, ``last_error`` or the recipient.
    """
    ids = list(object_ids)
    if not ids or object_type not in DELIVERY_OBJECT_TYPES or not ctx.has(Capability.ORG_MANAGE):
        return {}
    rows = EmailOutbox.objects.filter(
        kind=Kind.SECURITY, eligibility_object_type=object_type, eligibility_object_id__in=ids
    )
    if source_ref:
        rows = rows.filter(source_ref=source_ref)
    latest = (
        rows.order_by("eligibility_object_id", "-created_at", "-pk")
        .distinct("eligibility_object_id")
        .values_list("eligibility_object_id", "status", "sent_at", "created_at")
    )
    current = now()
    return {
        object_id: DeliveryStatus(
            state=status,
            at=sent_at or created_at,
            delayed=status == Status.QUEUED and current - created_at > DELAYED_AFTER,
        )
        for object_id, status, sent_at, created_at in latest
        if object_id is not None
    }
