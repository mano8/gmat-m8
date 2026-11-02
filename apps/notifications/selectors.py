"""Reads of the notification screens (M7.1) and the delivery badges (M7c.1)."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

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

# A message still queued or sending this long after it was queued: the worker is paused, down
# or retrying, or outbound mail is off. ``web`` does not know which (Q38); it says to ask.
DELAYED_AFTER = timedelta(minutes=10)

IN_FLIGHT = frozenset({Status.QUEUED, Status.SENDING})

# How often a badge in flight reloads itself; less often once delayed (the worker may resume).
POLL_EVERY = "5s"
POLL_EVERY_DELAYED = "60s"

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
RETRYING_LABEL = _("Retrying")
RETRYING_HINT = _("An attempt failed; the mail worker tries again.")


@dataclass(frozen=True)
class DeliveryStatus:
    """What a screen may show of one email: its state and when, never what it said."""

    state: str
    at: datetime  # when it was sent; else when it was queued
    delayed: bool  # still in flight ``DELAYED_AFTER`` after it was queued
    retrying: bool = False  # queued again after a failed attempt

    @property
    def label(self) -> str:
        if self.delayed:
            return str(DELAYED_LABEL)
        return str(RETRYING_LABEL if self.retrying else Status(self.state).label)

    @property
    def hint(self) -> str:
        if self.delayed:
            return str(DELAYED_HINT)
        return str(RETRYING_HINT if self.retrying else _HINTS.get(self.state, ""))

    @property
    def tone(self) -> str:
        return "warn" if self.delayed or self.retrying else _TONES.get(self.state, "")

    @property
    def polling(self) -> bool:
        """Whether the badge refreshes itself: only while the worker may still change it."""
        return self.state in IN_FLIGHT

    @property
    def poll_every(self) -> str:
        return POLL_EVERY_DELAYED if self.delayed else POLL_EVERY


def email_opt_out(ctx: AccessContext) -> bool:
    """Whether the signed-in user turned business email off."""
    if ctx.user_id is None:
        return False
    return NotificationPreference.objects.filter(user_id=ctx.user_id, email_opt_out=True).exists()


def delivery_status(
    ctx: AccessContext, object_type: str, object_ids: Iterable[UUID]
) -> dict[tuple[UUID, str], DeliveryStatus]:
    """The latest security email of each kind about each object, in one query (Q37).

    Keyed by ``(object id, source_ref)``: an invitation has one message (``invitation``), an
    email change two (its confirm and notice mails). The caller passes only objects it
    already read through its own scoped selector; a group administrator's ``org.manage`` is
    checked again here. Only the state, the attempt count and the times are selected: never
    the body, the sealed content, ``last_error`` or the recipient.
    """
    ids = list(object_ids)
    if not ids or object_type not in DELIVERY_OBJECT_TYPES or not ctx.has(Capability.ORG_MANAGE):
        return {}
    latest = (
        EmailOutbox.objects.filter(
            kind=Kind.SECURITY, eligibility_object_type=object_type, eligibility_object_id__in=ids
        )
        .order_by("eligibility_object_id", "source_ref", "-created_at", "-pk")
        .distinct("eligibility_object_id", "source_ref")
        .values_list(
            "eligibility_object_id", "source_ref", "status", "attempts", "sent_at", "created_at"
        )
    )
    current = now()
    return {
        (object_id, source_ref): DeliveryStatus(
            state=status,
            at=sent_at or created_at,
            delayed=status in IN_FLIGHT and current - created_at > DELAYED_AFTER,
            retrying=status == Status.QUEUED and attempts > 0,
        )
        for object_id, source_ref, status, attempts, sent_at, created_at in latest
        if object_id is not None
    }
