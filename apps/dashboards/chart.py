"""What a chart panel draws (plan M8b.5): counts and durations, never a price.

A ``Chart`` holds its labels and series once; the page gets them twice from it: as the JSON
of the canvas's ``data-chart`` attribute that ``static/js/charts.js`` draws (``payload``), and
as the table under the chart, which is also what shows without JavaScript (``rows``). Not a
``json_script`` block: the panels arrive through htmx, which removes every ``<script>`` from a
swapped fragment (``allowScriptTags: false``), so the data travels as an escaped attribute,
never executed either way. ``summary`` is the canvas's
accessible name: the main reading in one sentence.
"""

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from django.utils.formats import date_format
from django.utils.functional import Promise

from apps.access.context import AccessContext
from apps.core.stats import Calendar
from apps.dashboards.period import Period

BAR, LINE, HBAR, METERS = "bar", "line", "hbar", "meters"
WEEK, CATEGORY = "week", "category"


@dataclass(frozen=True)
class Series:
    label: str
    values: tuple[int, ...]

    @property
    def total(self) -> int:
        return sum(self.values)


@dataclass(frozen=True)
class Meter:
    """One bar of a ``METERS`` panel: ``value`` of ``maximum``, with a link."""

    label: str
    detail: str
    value: int
    maximum: int
    href: str


@dataclass(frozen=True)
class Chart:
    kind: str
    labels: tuple[str, ...] = ()
    series: tuple[Series, ...] = ()
    summary: str = ""
    hint: str = ""
    label_head: str = ""
    axis: str = CATEGORY
    stacked: bool = False
    meters: tuple[Meter, ...] = ()
    # The weeks behind ``labels`` when ``axis`` is ``WEEK`` (ISO dates in the payload).
    weeks: tuple[date, ...] = field(default=())

    @property
    def empty(self) -> bool:
        if self.kind == METERS:
            return not self.meters
        return not any(series.total for series in self.series)

    @property
    def payload(self) -> dict[str, Any]:
        labels = [day.isoformat() for day in self.weeks] if self.axis == WEEK else self.labels
        return {
            "type": self.kind,
            "axis": self.axis,
            "stacked": self.stacked,
            "labels": list(labels),
            "series": [{"label": s.label, "data": list(s.values)} for s in self.series],
        }

    @property
    def payload_json(self) -> str:
        """``payload`` as JSON for the canvas's ``data-chart`` attribute (escaped there)."""
        return json.dumps(self.payload, separators=(",", ":"))

    @property
    def rows(self) -> list[tuple[str, list[int]]]:
        return [
            (label, [series.values[index] for series in self.series])
            for index, label in enumerate(self.labels)
        ]


def weekly_chart(kind: str, calendar: Calendar, series: Sequence[Series], **more: Any) -> Chart:
    """A chart over the weeks of ``calendar``: the table names each week by its Monday."""
    labels = tuple(date_format(start, "MONTH_DAY_FORMAT") for start in calendar.starts)
    return Chart(
        kind,
        labels=labels,
        series=tuple(series),
        axis=WEEK,
        weeks=calendar.starts,
        **more,
    )


@dataclass(frozen=True)
class ChartPanel:
    """A dashboard panel that draws a chart; loaded lazily like the list panels."""

    key: str
    title: str | Promise
    capability: str
    build: Callable[[AccessContext, Period], Chart]
    link: str
    empty: str | Promise
    # More capabilities the panel reads with (each set through its own ``visible_to``).
    requires: tuple[str, ...] = ()
    query: str = ""
    wide: bool = False  # spans both columns of the charts grid on a wide screen
    kind: str = "chart"
