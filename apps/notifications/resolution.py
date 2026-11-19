"""Who wants a business notice on a channel: the four levels of Q93, in one query (M15.1, M17.1).

In order, for one person, one notice and one channel (``kinds.CHANNELS``):

1. the person's switch for the channel (:func:`turned_off`): off, nothing of any notice reaches
   them there. For email it is their *no business email* (``NotificationPreference``, M7.1);
   for another channel a ``ChannelSwitch`` row, or nothing, which means the channel's default
   (on, unless the channel is opt-in);
2. otherwise the person's own choice for the notice and channel (``NoticeChoice``), on or off:
   it wins over both administrators' levels, in both directions;
3. otherwise an administrator's setting for the person (``NoticeSetting``: ``ALWAYS`` or
   ``NEVER``);
4. otherwise the setting for everyone (``NoticeDefault``); no row means the channel's default.

``queue_notification`` applies it on email when a message is queued (nobody left: no message)
and the worker again at the claim (``eligibility.eligible_recipients``), so a queued, unclaimed
message follows a later change. A message without a notice (queued before M15) follows the
switch alone. Who may receive the message at all (active, still holding its capability on its
object) is ``eligibility``'s question, not this module's.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from django.db.models import (
    BooleanField,
    Case,
    Exists,
    F,
    OuterRef,
    Q,
    QuerySet,
    Subquery,
    Value,
    When,
)
from django.db.models.functions import Coalesce

from apps.accounts.models import User
from apps.notifications.kinds import CHANNELS, Channel, Mode, Notice
from apps.notifications.models import (
    ChannelSwitch,
    NoticeChoice,
    NoticeDefault,
    NoticeSetting,
    NotificationPreference,
)


def turned_off(channel: Channel) -> Q:
    """The users whose switch for ``channel`` is off: a condition on ``User`` rows.

    The one answer for every channel: email's switch is ``NotificationPreference.email_opt_out``
    (no row: on); another channel's is a ``ChannelSwitch`` row, and without one the channel's
    default (off only for an opt-in channel).
    """
    if channel == Channel.EMAIL:
        return Q(
            Exists(NotificationPreference.objects.filter(user=OuterRef("pk"), email_opt_out=True))
        )
    switches = ChannelSwitch.objects.filter(user=OuterRef("pk"), channel=channel)
    if CHANNELS[channel].opt_in:
        return ~Q(Exists(switches.filter(enabled=True)))
    return Q(Exists(switches.filter(enabled=False)))


def filter_wanted(
    users: QuerySet[User], notice: Notice | str, channel: Channel = Channel.EMAIL
) -> QuerySet[User]:
    """``users`` narrowed, in SQL, to those who want ``notice`` on ``channel``.

    ``notice`` may be blank (a message queued before M15): only the switch applies then.
    """
    users = users.exclude(turned_off(channel))
    if not notice:
        return users
    mine = {"user": OuterRef("pk"), "notice": notice, "channel": channel}
    choice = NoticeChoice.objects.filter(**mine).values("enabled")[:1]
    setting = NoticeSetting.objects.filter(**mine).values("mode")[:1]
    default = NoticeDefault.objects.filter(notice=notice, channel=channel).values("enabled")[:1]
    return (
        users.annotate(_notice_choice=Subquery(choice), _notice_mode=Subquery(setting))
        .annotate(
            _notice_wanted=Coalesce(
                F("_notice_choice"),
                Case(
                    When(_notice_mode=Mode.ALWAYS, then=Value(True)),
                    When(_notice_mode=Mode.NEVER, then=Value(False)),
                    output_field=BooleanField(),
                ),
                Subquery(default),
                Value(CHANNELS[channel].default),
                output_field=BooleanField(),
            )
        )
        .filter(_notice_wanted=True)
    )


def wanted(
    user_ids: Iterable[UUID], notice: Notice | str, channel: Channel = Channel.EMAIL
) -> set[UUID]:
    """The users among ``user_ids`` who want ``notice`` on ``channel`` (one query)."""
    ids = set(user_ids)
    if not ids:
        return set()
    found = filter_wanted(User.objects.filter(pk__in=ids), notice, channel)
    return set(found.values_list("pk", flat=True))


class Reason(StrEnum):
    """Which level decided a person's state for a notice on a channel (Q93)."""

    CHANNEL_OFF = "channel_off"  # the person turned the channel off
    OWN = "own"  # the person chose for this notice themselves
    PERSON = "person"  # an administrator set this notice for the person
    EVERYONE = "everyone"  # the setting for everyone, or none yet (the channel's default)


@dataclass(frozen=True)
class Decision:
    """One person's state for one notice on one channel.

    ``wanted`` is what the four levels give and ``reason`` the level that decided. ``choice``
    is the person's own choice (``None``: follow the administrators), ``mode`` an
    administrator's setting for the person (``None``: follow the setting for everyone), and
    ``administrators`` what the administrators' two levels give, which is what the person gets
    back by following them.
    """

    wanted: bool
    reason: Reason
    choice: bool | None
    mode: Mode | None
    administrators: bool


@dataclass(frozen=True)
class _Levels:
    """What one person has stored for a channel, read once for every notice."""

    off: bool
    choices: dict[str, bool]
    modes: dict[str, str]
    defaults: dict[str, bool]
    fallback: bool

    def decide(self, notice: Notice) -> Decision:
        choice = self.choices.get(notice)
        mode = Mode(self.modes[notice]) if notice in self.modes else None
        everyone = self.defaults.get(notice, self.fallback)
        administrators = mode == Mode.ALWAYS if mode is not None else everyone
        if self.off:
            return Decision(False, Reason.CHANNEL_OFF, choice, mode, administrators)
        if choice is not None:
            return Decision(choice, Reason.OWN, choice, mode, administrators)
        reason = Reason.PERSON if mode is not None else Reason.EVERYONE
        return Decision(administrators, reason, choice, mode, administrators)


def decisions(user_id: UUID, channel: Channel = Channel.EMAIL) -> dict[Notice, Decision]:
    """What ``wanted`` decides for ``user_id`` on ``channel``, for every notice, and why.

    For the screens that show a person their state (M15.3, M17.4); four queries, the same
    rule as ``wanted``, which a test keeps equal on every combination and every channel.
    """
    levels = _Levels(
        off=User.objects.filter(pk=user_id).filter(turned_off(channel)).exists(),
        choices=dict(
            NoticeChoice.objects.filter(user_id=user_id, channel=channel).values_list(
                "notice", "enabled"
            )
        ),
        modes=dict(
            NoticeSetting.objects.filter(user_id=user_id, channel=channel).values_list(
                "notice", "mode"
            )
        ),
        defaults=dict(
            NoticeDefault.objects.filter(channel=channel).values_list("notice", "enabled")
        ),
        fallback=CHANNELS[channel].default,
    )
    return {notice: levels.decide(notice) for notice in Notice}
