"""The period a dashboard shows (plan M8b.5): ``?weeks=4|12|26``, 12 by default.

The value lives in the URL only, like a list's page size and sort (ADR-0023); anything
outside the allowlist falls back to the default. The weeks end with the current one and
start on Monday in the installation's ``TIME_ZONE`` (``apps.core.stats``).
"""

from dataclasses import dataclass, field
from datetime import date, datetime

from django.http import HttpRequest
from django.utils import timezone

from apps.core.stats import Calendar

CHOICES = (4, 12, 26)
DEFAULT = 12


@dataclass(frozen=True)
class Period:
    weeks: int
    now: datetime
    calendar: Calendar = field(init=False)

    def __post_init__(self) -> None:
        local = timezone.localdate(self.now)
        object.__setattr__(self, "calendar", Calendar.ending_today(self.weeks, local))

    @property
    def today(self) -> date:
        return timezone.localdate(self.now)

    @property
    def since(self) -> datetime:
        return self.calendar.since

    @property
    def before(self) -> datetime:
        """The start of the period of the same length just before this one."""
        return Calendar.ending_today(self.weeks * 2, self.today).since


def weeks_of(request: HttpRequest) -> int:
    raw = request.GET.get("weeks", "")
    return int(raw) if raw in {str(choice) for choice in CHOICES} else DEFAULT


def period_of(request: HttpRequest, now: datetime | None = None) -> Period:
    return Period(weeks_of(request), now or timezone.now())
