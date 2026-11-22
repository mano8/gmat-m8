"""Reads of the notification screens (M7.1), the delivery badges (M7c.1) and the person's
in-app notifications (M17.3: the bell's count and the list, through ``in_app.visible``)."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from django.db.models import Count, OuterRef, Q, QuerySet, Subquery
from django.utils.functional import Promise
from django.utils.timezone import now
from django.utils.translation import gettext_lazy as _

from apps.access.context import AccessContext
from apps.access.policy import is_staff_account
from apps.access.roles import Capability
from apps.accounts.models import User
from apps.material_requests.models import MaterialRequest
from apps.notifications import in_app, resolution
from apps.notifications.kinds import CHANNELS, NOTICES, Channel, ChannelSpec, Mode, NoticeSpec
from apps.notifications.models import (
    DISPATCH_KINDS,
    EmailOutbox,
    InAppNotification,
    Kind,
    NoticeDefault,
    NoticeSetting,
    Status,
)
from apps.notifications.resolution import Reason

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


def quarantined_counts() -> dict[str, int]:
    """Held messages per kind (``manage outbox_quarantine``, a shell's view of the outbox)."""
    rows = EmailOutbox.objects.filter(status=Status.QUARANTINED).order_by("kind")
    return {row["kind"]: row["n"] for row in rows.values("kind").annotate(n=Count("pk"))}


def quarantined_dispatches() -> QuerySet[EmailOutbox]:
    """Held supplier dispatches: a human decides on each after checking with the supplier."""
    return (
        EmailOutbox.objects.filter(status=Status.QUARANTINED, kind__in=DISPATCH_KINDS)
        .select_related("source_po")
        .order_by("created_at", "pk")
    )


# --- the business notices' settings (M15.3, M17.4: Q87-Q89, Q93, Q97) -----------------------


@dataclass(frozen=True)
class DefaultCell:
    """One notice on one channel as the administration page shows it: whether it is sent to
    everyone who has no setting of their own and how many people (active accounts) have one,
    split by what it says."""

    channel: Channel
    enabled: bool
    always: int
    never: int

    @property
    def own(self) -> int:
        return self.always + self.never

    @property
    def label(self) -> str:
        return CHANNELS[self.channel].label


@dataclass(frozen=True)
class NoticeState:
    """A notice as the administration page shows it: its state on each channel."""

    spec: NoticeSpec
    cells: tuple[DefaultCell, ...]

    def cell(self, channel: Channel) -> DefaultCell:
        return next(cell for cell in self.cells if cell.channel == channel)


def notice_states(ctx: AccessContext) -> list[NoticeState]:
    """Every notice in the registry's order with its setting for everyone and the people who
    have their own, on every channel (``org.manage`` only; nothing for anyone else)."""
    if not ctx.has(Capability.ORG_MANAGE):
        return []
    defaults = {
        (notice, channel): enabled
        for notice, channel, enabled in NoticeDefault.objects.values_list(
            "notice", "channel", "enabled"
        )
    }
    counts = {
        (row["notice"], row["channel"]): (row["always"], row["never"])
        for row in NoticeSetting.objects.filter(
            user__is_active=True, user__offboarded_at__isnull=True
        )
        .values("notice", "channel")
        .annotate(
            always=Count("pk", filter=Q(mode=Mode.ALWAYS)),
            never=Count("pk", filter=Q(mode=Mode.NEVER)),
        )
    }
    return [
        NoticeState(
            spec,
            tuple(
                DefaultCell(
                    channel,
                    defaults.get((notice, channel), channel_spec.default),
                    *counts.get((notice, channel), (0, 0)),
                )
                for channel, channel_spec in CHANNELS.items()
            ),
        )
        for notice, spec in NOTICES.items()
    ]


@dataclass(frozen=True)
class PersonNotice:
    """One notice on one channel for one person: what applies and which level decided, the
    person's own choice (``None``: they follow the administrators), an administrator's setting
    for them, and what the administrators' levels give (what following them would give)."""

    channel: Channel
    wanted: bool
    reason: Reason
    choice: bool | None
    mode: Mode | None
    administrators: bool

    @property
    def label(self) -> str:
        return CHANNELS[self.channel].label

    @property
    def overrides(self) -> bool:
        """The person's own choice differs from what the administrators set for them."""
        return self.reason == Reason.OWN and self.choice != self.administrators

    @property
    def off(self) -> bool:
        """The person turned the whole channel off."""
        return self.reason == Reason.CHANNEL_OFF


@dataclass(frozen=True)
class NoticeRow:
    """One notice for one person on every channel, and whether their current roles receive it at
    all (a notice is queued only for who holds its capability)."""

    spec: NoticeSpec
    receivable: bool
    cells: tuple[PersonNotice, ...]

    def cell(self, channel: Channel) -> PersonNotice:
        return next(cell for cell in self.cells if cell.channel == channel)


def _person_notices(person: User) -> list[NoticeRow]:
    held = AccessContext.for_user(person)
    by_channel = {channel: resolution.decisions(person.pk, channel) for channel in CHANNELS}
    return [
        NoticeRow(
            spec,
            held.has(spec.capability),
            tuple(
                PersonNotice(
                    channel,
                    decided[notice].wanted,
                    decided[notice].reason,
                    decided[notice].choice,
                    decided[notice].mode,
                    decided[notice].administrators,
                )
                for channel, decided in by_channel.items()
            ),
        )
        for notice, spec in NOTICES.items()
    ]


def person_notices(ctx: AccessContext, person: User) -> list[NoticeRow]:
    """Every notice for ``person`` on every channel (``org.manage``; a staff account's settings
    are not shown: nobody changes them)."""
    if not ctx.has(Capability.ORG_MANAGE) or is_staff_account(person):
        return []
    return _person_notices(person)


def own_notices(ctx: AccessContext) -> list[NoticeRow]:
    """Every notice for the signed-in user on every channel: their own state, nobody else's."""
    if ctx.user is None:
        return []
    return _person_notices(ctx.user)


@dataclass(frozen=True)
class ChannelState:
    """A channel and whether the person receives anything on it."""

    spec: ChannelSpec
    enabled: bool


def own_channels(ctx: AccessContext) -> list[ChannelState]:
    """The signed-in person's switch per channel, in the registry's order (level 1 of Q93)."""
    if ctx.user_id is None:
        return []
    mine = User.objects.filter(pk=ctx.user_id)
    return [
        ChannelState(spec, not mine.filter(resolution.turned_off(channel)).exists())
        for channel, spec in CHANNELS.items()
    ]


# --- in-app notifications (M17.3, Q99) -------------------------------------------------------

UNREAD_CAP = 99  # the bell's count stops here and reads "99+" (as a dashboard's, M7d.4)


@dataclass(frozen=True)
class Unread:
    """The bell's count: the person's unread notifications they may still see, up to the cap."""

    count: int
    more: bool

    @property
    def label(self) -> str:
        return f"{UNREAD_CAP}+" if self.more else str(self.count)


def unread_count(ctx: AccessContext) -> Unread:
    """How many of the person's notifications are unread, counting at most ``UNREAD_CAP + 1`` rows
    (one ``COUNT`` over a ``LIMIT``ed subquery: the cost of the cap, never of the backlog)."""
    found = in_app.visible(ctx).filter(read_at__isnull=True).values("pk")[: UNREAD_CAP + 1].count()
    return Unread(count=min(found, UNREAD_CAP), more=found > UNREAD_CAP)


def in_app_notifications(ctx: AccessContext) -> QuerySet[InAppNotification]:
    """The person's notifications they may still see, newest first, each with the number of its
    request (``number``), for ``apps.core.listing.paginate``: the database pages it."""
    number = MaterialRequest.objects.filter(pk=OuterRef("object_id")).values("number")[:1]
    return in_app.visible(ctx).annotate(number=Subquery(number)).order_by("-notified_at", "-pk")
