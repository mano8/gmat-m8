"""Who wants a business notice: the three levels of Q87, in one query (M15.1).

In order, for one person, one notice and one channel:

1. the person's own refusal of business email (``NotificationPreference.email_opt_out``,
   M7.1; an email setting) wins: nobody sends them business email, whatever an
   administrator set;
2. otherwise the person's own setting for the notice (``NoticeSetting``: ``ALWAYS`` or
   ``NEVER``), set by a group administrator;
3. otherwise the setting for everyone (``NoticeDefault``); no row means on.

``queue_notification`` applies it when a message is queued (nobody left: no message) and the
worker again at the claim (``eligibility.eligible_recipients``), so a queued, unclaimed
message follows a later change. A message without a notice (queued before M15) follows the
refusal alone. Who may receive the message at all (active, still holding its capability on
its object) is ``eligibility``'s question, not this module's.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from django.db.models import BooleanField, Exists, OuterRef, Q, QuerySet, Subquery, Value
from django.db.models.functions import Coalesce

from apps.accounts.models import User
from apps.notifications.kinds import Channel, Mode, Notice
from apps.notifications.models import NoticeDefault, NoticeSetting, NotificationPreference


def _refused() -> Q:
    """The users who turned business email off (their *no business email*, M7.1).

    Email is the only channel (Q90). A channel added later (the Telegram bot) brings its own
    consent, checked next to this one.
    """
    return Q(Exists(NotificationPreference.objects.filter(user=OuterRef("pk"), email_opt_out=True)))


def filter_wanted(
    users: QuerySet[User], notice: Notice | str, channel: Channel = Channel.EMAIL
) -> QuerySet[User]:
    """``users`` narrowed, in SQL, to those who want ``notice`` on ``channel``.

    ``notice`` may be blank (a message queued before M15): only the refusal applies then.
    """
    users = users.exclude(_refused())
    if not notice:
        return users
    setting = NoticeSetting.objects.filter(
        user=OuterRef("pk"), notice=notice, channel=channel
    ).values("mode")[:1]
    default = NoticeDefault.objects.filter(notice=notice, channel=channel).values("enabled")[:1]
    return users.annotate(
        _notice_mode=Subquery(setting),
        _notice_default=Coalesce(Subquery(default), Value(True), output_field=BooleanField()),
    ).filter(Q(_notice_mode=Mode.ALWAYS) | Q(_notice_mode__isnull=True, _notice_default=True))


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
    """Which level decided a person's state for a notice."""

    OPTED_OUT = "opted_out"  # the person turned business email off
    PERSON = "person"  # an administrator set this notice for the person
    EVERYONE = "everyone"  # the setting for everyone, or none yet (on)


@dataclass(frozen=True)
class Decision:
    """One person's state for one notice: ``mode`` is their own setting (``None``: follow the
    setting for everyone), ``wanted`` what the three levels give, ``reason`` the level."""

    wanted: bool
    reason: Reason
    mode: Mode | None


def decisions(user_id: UUID, channel: Channel = Channel.EMAIL) -> dict[Notice, Decision]:
    """What ``wanted`` decides for ``user_id``, for every notice, and why (three queries).

    For the screens that show a person their state (M15.3); the same rule as ``wanted``, which
    a test keeps equal.
    """
    refused = NotificationPreference.objects.filter(user_id=user_id, email_opt_out=True).exists()
    modes = dict(
        NoticeSetting.objects.filter(user_id=user_id, channel=channel).values_list("notice", "mode")
    )
    defaults = dict(NoticeDefault.objects.filter(channel=channel).values_list("notice", "enabled"))
    found: dict[Notice, Decision] = {}
    for notice in Notice:
        mode = Mode(modes[notice]) if notice in modes else None
        if refused:
            found[notice] = Decision(False, Reason.OPTED_OUT, mode)
        elif mode is not None:
            found[notice] = Decision(mode == Mode.ALWAYS, Reason.PERSON, mode)
        else:
            found[notice] = Decision(defaults.get(notice, True), Reason.EVERYONE, None)
    return found
