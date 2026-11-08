"""The dashboard's key figures (plan M8b.5): at most four ``stat_tile``s for the user's role.

The specs are ranked; the user gets the first four whose capabilities they hold, so a
reviewer sees the inbox first, a buyer what is left to purchase, a requester their own
requests. Each tile links to the list it counts, filtered when the list can be; a count
worked out from the ledger stops at "99+" (``apps.core.listing.head``), like the panels.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from functools import cached_property

from django.urls import reverse
from django.utils.formats import date_format, number_format
from django.utils.translation import gettext as _

from apps.access.context import AccessContext
from apps.access.roles import Capability
from apps.core.listing import head
from apps.dashboards.period import Period
from apps.handovers import selectors as handover_selectors
from apps.material_requests import selectors as request_selectors
from apps.purchasing import selectors as purchasing_selectors

MOST = 4
COUNT_CAP = 99
UP, DOWN, FLAT = "up", "down", "flat"
HOUR, DAY = timedelta(hours=1), timedelta(days=1)
Status = request_selectors.Status


@dataclass(frozen=True)
class Tile:
    key: str
    label: str
    value: str
    href: str
    icon: str
    note: str = ""
    change: str = ""
    direction: str = ""
    good: bool | None = None
    spark: tuple[float, ...] = field(default=())


class Facts:
    """What several tiles share, read once per page."""

    def __init__(self, ctx: AccessContext, period: Period) -> None:
        self.ctx, self.period = ctx, period

    @cached_property
    def own(self) -> request_selectors.OwnCounts:
        return request_selectors.own_counts(self.ctx, now=self.period.now)

    @cached_property
    def ages(self) -> tuple[int, int, int, int]:
        return request_selectors.inbox_ages(self.ctx, now=self.period.now)

    @cached_property
    def overdue(self) -> purchasing_selectors.Overdue:
        return purchasing_selectors.overdue_summary(self.ctx, today=self.period.today)


def _capped(rows: list[object], more: bool) -> str:
    return f"{len(rows)}+" if more else str(len(rows))


def duration(value: timedelta) -> str:
    """A time to a decision, short: minutes under an hour, hours under two days, days."""
    if value < HOUR:
        minutes = max(1, round(value.total_seconds() / 60))
        return _("%(n)d min") % {"n": minutes}
    if value < 2 * DAY:
        return _("%(n)d h") % {"n": round(value.total_seconds() / 3600)}
    days = round(value.total_seconds() / 86400, 1)
    return _("%(n)s d") % {"n": number_format(days, decimal_pos=1 if days % 1 else 0)}


# --- the tiles -----------------------------------------------------------------------------


def waiting(facts: Facts) -> Tile:
    old = facts.ages[-1]
    return Tile(
        "inbox",
        _("Waiting for review"),
        str(sum(facts.ages)),
        reverse("requests:inbox"),
        "inbox",
        note=_("%(n)d for more than a week") % {"n": old},
    )


def _compare(now: timedelta, before: timedelta | None, weeks: int) -> tuple[str, str, bool | None]:
    if before is None:
        return _("Nothing to compare with yet"), FLAT, None
    gap = now - before
    if abs(gap) < HOUR:
        return _("As in the %(weeks)d weeks before") % {"weeks": weeks}, FLAT, None
    words = {"time": duration(abs(gap)), "weeks": weeks}
    if gap < timedelta(0):
        return _("%(time)s faster than the %(weeks)d weeks before") % words, DOWN, True
    return _("%(time)s slower than the %(weeks)d weeks before") % words, UP, False


def decision_time(facts: Facts) -> Tile:
    period = facts.period
    now = request_selectors.median_decision_time(facts.ctx, since=period.since, until=period.now)
    href, label = reverse("requests:inbox"), _("Median time to a decision")
    if now is None:
        return Tile("decision-time", label, _("None yet"), href, "clock")
    before = request_selectors.median_decision_time(
        facts.ctx, since=period.before, until=period.since
    )
    change, direction, good = _compare(now, before, period.weeks)
    weekly = request_selectors.median_by_week(facts.ctx, since=period.since)
    spark = tuple(
        weekly[start].total_seconds() / 3600 for start in period.calendar.starts if start in weekly
    )
    return Tile(
        "decision-time", label, duration(now), href, "clock", "", change, direction, good, spark
    )


def to_purchase(facts: Facts) -> Tile:
    worklist = purchasing_selectors.Worklist(facts.ctx)
    rows, more = head(worklist.seek, worklist.line_window, limit=COUNT_CAP)
    return Tile(
        "to-purchase",
        _("To purchase"),
        _capped(rows, more),
        reverse("purchasing:worklist"),
        "shopping-cart",
        note=_("approved lines still to buy"),
    )


def overdue(facts: Facts) -> Tile:
    found = facts.overdue
    note = (
        _("expected since %(day)s") % {"day": date_format(found.oldest, "SHORT_DATE_FORMAT")}
        if found.oldest
        else _("none past its expected date")
    )
    return Tile(
        "overdue",
        _("Overdue orders"),
        str(found.count),
        f"{reverse('purchasing:orders')}?status=ISSUED",
        "triangle-alert",
        note=note,
    )


def awaiting_receipt(facts: Facts) -> Tile:
    count = purchasing_selectors.awaiting_receipt(facts.ctx).count()
    return Tile(
        "awaiting-receipt",
        _("Awaiting receipt"),
        str(count),
        f"{reverse('purchasing:orders')}?status=ISSUED",
        "truck",
        note=_("issued orders still to receive"),
    )


def to_hand_over(facts: Facts) -> Tile:
    awaiting = handover_selectors.AwaitingHandover(facts.ctx)
    rows, more = head(awaiting.seek, awaiting.window, limit=COUNT_CAP)
    return Tile(
        "to-hand-over",
        _("To hand over"),
        _capped(rows, more),
        reverse("handovers:new"),
        "hand-helping",
        note=_("lines with goods to hand over"),
    )


def _own(key: str, label: str, status: str, count: int, icon: str, note: str = "") -> Tile:
    href = f"{reverse('requests:list')}?status={status}"
    return Tile(key, label, str(count), href, icon, note=note)


def drafts(facts: Facts) -> Tile:
    return _own("drafts", _("My drafts"), Status.DRAFT, facts.own.drafts, "square-pen")


def submitted(facts: Facts) -> Tile:
    count = facts.own.submitted
    return _own("submitted", _("Submitted"), Status.SUBMITTED, count, "send")


def approved(facts: Facts) -> Tile:
    count = facts.own.approved
    return _own("approved", _("Approved"), Status.APPROVED, count, "circle-check")


def closed(facts: Facts) -> Tile:
    note = _("in the last 30 days")
    return _own("closed", _("Closed"), Status.CLOSED, facts.own.closed, "clipboard-check", note)


# --- the ranking ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TileSpec:
    capability: str
    build: Callable[[Facts], Tile]


C = Capability
TILES: tuple[TileSpec, ...] = (
    TileSpec(C.REQUEST_REVIEW, waiting),
    TileSpec(C.REQUEST_REVIEW, decision_time),
    TileSpec(C.PO_MANAGE, to_purchase),
    TileSpec(C.PO_VIEW, overdue),
    TileSpec(C.PO_VIEW, awaiting_receipt),
    TileSpec(C.HANDOVER_POST, to_hand_over),
    TileSpec(C.REQUEST_CREATE, drafts),
    TileSpec(C.REQUEST_CREATE, submitted),
    TileSpec(C.REQUEST_CREATE, approved),
    TileSpec(C.REQUEST_CREATE, closed),
)


def held(ctx: AccessContext) -> list[TileSpec]:
    """The specs of the user's tiles: the first ``MOST`` whose capability they hold."""
    return [spec for spec in TILES if ctx.has(spec.capability)][:MOST]


def tiles(ctx: AccessContext, period: Period) -> list[Tile]:
    facts = Facts(ctx, period)
    return [spec.build(facts) for spec in held(ctx)]
