"""Reads of the audit trail (plan M7.3): every row through ``visible_to(ctx, audit.view)``.

An event belongs to the branch recorded with it: material managers read the events of their
branches, group administrators all of them, among them the ones without a branch. The payload
(``data``) and the client address are never selected for display: a diff can hold prices and
the address is personal data nobody needs to read to follow what happened.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from uuid import UUID

from django.db.models import Q, QuerySet
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.access.context import AccessContext
from apps.access.exceptions import NotInScope
from apps.access.roles import Capability
from apps.audit.models import AuditEvent

VIEW = Capability.AUDIT_VIEW
DEFAULT_PER_PAGE = 50
EVENT_SORTS = {"time": ("occurred_at",)}
EVENT_SORT_LABELS = {"time": _("When")}
DISPLAY_FIELDS = (
    "id",
    "occurred_at",
    "actor_label",
    "via",
    "action",
    "object_type",
    "object_id",
)


@dataclass(frozen=True)
class EventFilter:
    """What the list narrows to; every field is optional."""

    q: str = ""
    actor: str = ""
    object_type: str = ""
    since: date | None = None
    until: date | None = None


def can_view(ctx: AccessContext) -> bool:
    return ctx.has(VIEW)


def events(ctx: AccessContext, narrow: EventFilter | None = None) -> QuerySet[AuditEvent]:
    """The events in scope, newest first; ``q`` looks at the actor, the action and the channel."""
    return _narrowed(AuditEvent.objects.visible_to(ctx, VIEW).only(*DISPLAY_FIELDS), narrow)


def _narrowed(rows: QuerySet[AuditEvent], narrow: EventFilter | None) -> QuerySet[AuditEvent]:
    narrow = narrow or EventFilter()
    if narrow.q:
        rows = rows.filter(
            Q(actor_label__icontains=narrow.q)
            | Q(action__icontains=narrow.q)
            | Q(via__icontains=narrow.q)
        )
    if narrow.actor:
        rows = rows.filter(actor_label__icontains=narrow.actor)
    if narrow.object_type:
        rows = rows.filter(object_type=narrow.object_type)
    if narrow.since is not None:
        rows = rows.filter(occurred_at__gte=_day_start(narrow.since))
    if narrow.until is not None:
        rows = rows.filter(occurred_at__lt=_day_start(narrow.until + timedelta(days=1)))
    return rows


def object_types(ctx: AccessContext) -> list[str]:
    """The kinds of object the user's events are about, for the filter."""
    found = (
        AuditEvent.objects.visible_to(ctx, VIEW)
        .exclude(object_type="")
        .order_by("object_type")
        .values_list("object_type", flat=True)
        .distinct()
    )
    return list(found)


def object_history(
    ctx: AccessContext, object_type: str, object_id: UUID, narrow: EventFilter | None = None
) -> QuerySet[AuditEvent]:
    """Everything that happened to one object, oldest first; none in scope is a 404.

    ``narrow`` filters the events shown, never whether the object is in scope: a filter that
    matches nothing is an empty page, not a 404.
    """
    rows = events(ctx).filter(object_type=object_type, object_id=object_id)
    if not rows.exists():
        raise NotInScope(VIEW)
    return _narrowed(rows, narrow).order_by("occurred_at", "id")


def _day_start(day: date) -> datetime:
    return timezone.make_aware(datetime.combine(day, time.min))
