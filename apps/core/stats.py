"""Counts per week and medians, worked out in the database (plan M8b.5).

A week starts on Monday at midnight in the installation's ``TIME_ZONE`` (PostgreSQL's
``date_trunc('week')`` in the current time zone, which Django sets from it), so a document
posted at 00:30 on a Monday morning belongs to that Monday's week whatever the offset that
day, daylight-saving changes included. The calendar of a period is built here from today's
date, never from the rows: a week with nothing in it is a zero, not a gap.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from django.db.models import Aggregate, Count, DurationField, ExpressionWrapper, F, QuerySet
from django.db.models.functions import TruncWeek
from django.utils import timezone

WEEK = timedelta(days=7)


@dataclass(frozen=True)
class Calendar:
    """The ``weeks`` weeks that end with the current one, oldest first."""

    starts: tuple[date, ...]

    @classmethod
    def ending_today(cls, weeks: int, today: date | None = None) -> Calendar:
        day = today or timezone.localdate()
        current = day - timedelta(days=day.weekday())
        return cls(tuple(current - WEEK * back for back in range(weeks - 1, -1, -1)))

    @property
    def since(self) -> datetime:
        """Midnight, local time, at the start of the first week (an aware datetime)."""
        return midnight(self.starts[0])

    def fill(self, found: dict[date, int]) -> list[int]:
        """The count of every week of the calendar, zero where nothing was found."""
        return [found.get(start, 0) for start in self.starts]


def midnight(day: date) -> datetime:
    return timezone.make_aware(datetime.combine(day, time.min))


def _week_start(value: datetime) -> date:
    """The Monday of a ``TruncWeek`` (an aware datetime at local midnight)."""
    return timezone.localtime(value).date()


def weekly(rows: QuerySet[Any], field: str, since: datetime) -> dict[date, int]:
    """How many of ``rows`` fall in each week from ``since`` on, by ``field`` (a datetime)."""
    return weekly_aggregate(rows, field, since, Count("pk", distinct=True))


def weekly_aggregate(
    rows: QuerySet[Any], field: str, since: datetime, aggregate: Aggregate
) -> dict[date, Any]:
    """``aggregate`` over the rows of each week from ``since`` on, by ``field``."""
    found = (
        rows.filter(**{f"{field}__gte": since})
        .annotate(week=TruncWeek(field))
        .order_by()
        .values("week")
        .annotate(value=aggregate)
        .values_list("week", "value")
    )
    return {_week_start(week): value for week, value in found}


def weekly_by(
    rows: QuerySet[Any], field: str, since: datetime, key: str
) -> dict[tuple[date, Any], int]:
    """``weekly`` split by ``key`` (a field or an annotation of ``rows``)."""
    found = (
        rows.filter(**{f"{field}__gte": since})
        .annotate(week=TruncWeek(field))
        .order_by()
        .values("week", key)
        .annotate(total=Count("pk", distinct=True))
        .values_list("week", key, "total")
    )
    return {(_week_start(week), value): total for week, value, total in found}


class Median(Aggregate):
    """The median of an expression (PostgreSQL's ``percentile_cont(0.5)``)."""

    function = "PERCENTILE_CONT"
    name = "Median"
    template = "%(function)s(0.5) WITHIN GROUP (ORDER BY %(expressions)s)"
    allow_distinct = False


def median_duration(start: str, end: str) -> Median:
    """The median of ``end - start`` (two datetime fields) as a ``timedelta``."""
    return Median(
        ExpressionWrapper(F(end) - F(start), output_field=DurationField()),
        output_field=DurationField(),
    )


def top(counts: Iterable[tuple[Any, int]], limit: int) -> tuple[list[tuple[Any, int]], int]:
    """The ``limit`` largest counts, largest first (ties by key), and the sum of the rest."""
    ordered = sorted(counts, key=lambda item: (-item[1], str(item[0])))
    return ordered[:limit], sum(total for _, total in ordered[limit:])
