"""In-app notifications: written with the business change, checked again when shown (M17.3).

- :func:`notify` writes one ``InAppNotification`` per person who wants the notice on
  ``Channel.IN_APP`` (``resolution``: their switch, their own choice, the administrators' two
  levels; Q93), in the transaction of the change that causes it (``notices``), so a rolled-back
  change leaves none. The recipients are those the email goes to (``recipients``: active, holding
  the notice's capability on the request). A grouped notice (``models.GROUPED_IN_APP``: a
  reviewer's edits) refreshes the person's unread row about that request instead of adding one
  (Q98).
- :func:`visible` is the one query every read and every change of a person's notifications goes
  through: their own rows only, and of those only the ones whose request they may still see with
  the notice's capability, decided inside the query (``visible_to``), never in Python over the
  whole set (Q99). Nothing is stored but the notice, the request's id and the times, so a row
  whose request left the person's scope says nothing and counts for nothing.

Like ``outbox``, :func:`notify` authorizes nothing itself: the service that publishes the event
has run ``authorize_in_txn`` for its own operation.
"""

from collections import defaultdict
from collections.abc import Iterable

from django.db import DEFAULT_DB_ALIAS, connections
from django.db.models import Model, Q, QuerySet
from django.db.models.functions import Now

from apps.access.context import AccessContext
from apps.accounts.models import User
from apps.material_requests.models import MaterialRequest
from apps.notifications import eligibility, resolution
from apps.notifications.kinds import NOTICES, SUBJECT, Channel, Notice
from apps.notifications.models import GROUPED_IN_APP, InAppNotification
from apps.notifications.outbox import OutboxOutsideTransaction


def notify(notice: Notice, users: Iterable[User], obj: Model) -> int:
    """Tell ``users`` about ``notice`` on ``obj`` in the application, those who want it on
    ``Channel.IN_APP``; return how many rows were written or refreshed.

    ``obj`` must be what the notices are about (``kinds.SUBJECT``). Runs inside the caller's
    transaction. For a grouped notice, an unread row of the person about ``obj`` is moved to now
    (locked first, so a concurrent *mark as read* waits and then reads it again) and no row is
    added for them.
    """
    if not connections[DEFAULT_DB_ALIAS].in_atomic_block:
        raise OutboxOutsideTransaction("Write in-app notifications inside the service transaction")
    if notice not in set(Notice):
        raise ValueError(f"Unknown notice {notice!r}")
    object_type, object_id = eligibility.object_reference(obj)
    if object_type != SUBJECT:
        raise TypeError(f"{type(obj).__name__} is not what notices are about")
    ids = resolution.wanted((user.pk for user in users), notice, Channel.IN_APP)
    if not ids:
        return 0
    about = {"notice": notice, "object_type": object_type, "object_id": object_id}
    refreshed: set[object] = set()
    if notice in GROUPED_IN_APP:
        unread = InAppNotification.objects.filter(user_id__in=ids, read_at__isnull=True, **about)
        refreshed = set(unread.select_for_update().values_list("user_id", flat=True))
        unread.update(notified_at=Now())
    InAppNotification.objects.bulk_create(
        InAppNotification(user_id=user_id, **about) for user_id in sorted(ids - refreshed, key=str)
    )
    return len(ids)


def _notices_by_capability() -> dict[str, list[Notice]]:
    found: defaultdict[str, list[Notice]] = defaultdict(list)
    for notice, spec in NOTICES.items():
        found[spec.capability].append(notice)
    return dict(found)


def visible(ctx: AccessContext) -> QuerySet[InAppNotification]:
    """The person's own notifications whose request they may still see with the notice's
    capability (one query; nothing for a signed-out context)."""
    if ctx.user_id is None:
        return InAppNotification.objects.none()
    reach = Q()
    for capability, notices in _notices_by_capability().items():
        requests = MaterialRequest.objects.visible_to(ctx, capability).values("pk")
        reach |= Q(notice__in=notices, object_id__in=requests)
    return InAppNotification.objects.filter(reach, user_id=ctx.user_id, object_type=SUBJECT)
