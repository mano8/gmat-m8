"""Counts per week and medians in the database (plan M8b.5): the calendar of a period, weeks
that start on Monday in the installation's time zone (daylight-saving changes included), empty
weeks filled from the calendar, and the median of a duration."""

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from django.test import override_settings
from django.utils import timezone

from apps.core.stats import Calendar, median_duration, midnight, top, weekly, weekly_by
from apps.projects.models import Project
from tests.org import Org

pytestmark = pytest.mark.django_db

MADRID = ZoneInfo("Europe/Madrid")


class TestCalendar:
    def test_the_weeks_end_with_the_current_one_and_start_on_monday(self) -> None:
        calendar = Calendar.ending_today(4, date(2026, 11, 8))  # a Sunday
        assert calendar.starts == (
            date(2026, 10, 12),
            date(2026, 10, 19),
            date(2026, 10, 26),
            date(2026, 11, 2),
        )

    def test_a_monday_is_the_first_day_of_its_own_week(self) -> None:
        assert Calendar.ending_today(1, date(2026, 11, 9)).starts == (date(2026, 11, 9),)

    def test_since_is_local_midnight_of_the_first_monday(self) -> None:
        with override_settings(TIME_ZONE="Europe/Madrid"):
            since = Calendar.ending_today(2, date(2026, 11, 8)).since
            assert since == datetime(2026, 10, 26, tzinfo=MADRID)
            assert since == midnight(date(2026, 10, 26))

    def test_fill_gives_a_zero_to_every_week_without_rows(self) -> None:
        calendar = Calendar.ending_today(3, date(2026, 11, 8))
        assert calendar.fill({date(2026, 10, 26): 4}) == [0, 4, 0]


def _project(org: Org, code: str, at: datetime) -> Project:
    return Project.objects.create(
        code=code,
        name=code,
        branch=org.a_mad,
        procurement_branch=org.a_mad,
        status=Project.Status.ACTIVE,
        created_at=at,
    )


@pytest.fixture
def madrid(settings: Any) -> None:
    settings.TIME_ZONE = "Europe/Madrid"


@pytest.mark.usefixtures("madrid")
class TestWeekly:
    def test_a_week_starts_at_local_midnight_across_the_autumn_change(self, org: Org) -> None:
        # Summer time ended on Sunday 25 October 2026 at 03:00 (CEST → CET).
        _project(org, "SUN", datetime(2026, 10, 25, 23, 30, tzinfo=MADRID))  # 22:30 UTC
        _project(org, "MON", datetime(2026, 10, 26, 0, 30, tzinfo=MADRID))  # 23:30 UTC Sun
        found = weekly(
            Project.objects.filter(code__in=["SUN", "MON"]),
            "created_at",
            datetime(2026, 10, 1, tzinfo=MADRID),
        )
        assert found == {date(2026, 10, 19): 1, date(2026, 10, 26): 1}

    def test_and_across_the_spring_change(self, org: Org) -> None:
        # Summer time began on Sunday 29 March 2026 at 02:00 (CET → CEST).
        _project(org, "MON", datetime(2026, 3, 30, 0, 30, tzinfo=MADRID))  # 22:30 UTC Sun
        found = weekly(
            Project.objects.filter(code="MON"), "created_at", datetime(2026, 3, 1, tzinfo=MADRID)
        )
        assert found == {date(2026, 3, 30): 1}

    def test_rows_before_since_are_left_out_and_split_by_a_key(self, org: Org) -> None:
        _project(org, "OLD", datetime(2026, 9, 1, 12, tzinfo=MADRID))
        _project(org, "NEW", datetime(2026, 10, 27, 12, tzinfo=MADRID))
        rows = Project.objects.filter(code__in=["OLD", "NEW"])
        since = datetime(2026, 10, 1, tzinfo=MADRID)
        assert weekly(rows, "created_at", since) == {date(2026, 10, 26): 1}
        assert weekly_by(rows, "created_at", since, "code") == {(date(2026, 10, 26), "NEW"): 1}


class TestMedian:
    def test_the_median_of_a_duration(self, org: Org) -> None:
        start = timezone.now() - timedelta(days=30)
        for code, hours in (("P1", 1), ("P2", 3), ("P3", 10)):
            project = _project(org, code, start)
            Project.objects.filter(pk=project.pk).update(updated_at=start + timedelta(hours=hours))
        found = Project.objects.filter(code__in=["P1", "P2", "P3"]).aggregate(
            median=median_duration("created_at", "updated_at")
        )
        assert found["median"] == timedelta(hours=3)

    def test_no_row_has_no_median(self) -> None:
        found = Project.objects.none().aggregate(median=median_duration("created_at", "updated_at"))
        assert found["median"] is None


def test_top_keeps_the_largest_and_sums_the_rest() -> None:
    shown, rest = top([("b", 2), ("a", 2), ("c", 5), ("d", 1)], 2)
    assert shown == [("c", 5), ("a", 2)]
    assert rest == 3
