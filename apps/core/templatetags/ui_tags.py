"""Template helpers of the UI shell: icons, status chips, sparklines and a few filters."""

import json
from collections.abc import Sequence
from decimal import Decimal
from functools import cache, lru_cache
from typing import Any

from django import template
from django.conf import settings
from django.utils.formats import number_format
from django.utils.html import format_html, format_html_join
from django.utils.safestring import SafeString

register = template.Library()

SPARK_WIDTH, SPARK_HEIGHT, SPARK_MIN_POINTS = 84, 28, 2

#: The vendored Lucide subset (``static/vendor/lucide``, ADR-0027): per icon the shapes drawn in a
#: 24 x 24 box, read once. The markup is built with escaped attributes, never marked safe.
ICONS_FILE = settings.BASE_DIR / "static" / "vendor" / "lucide" / "icons.json"
#: Names the first hand-drawn icon set used (``components/icon.html``), now drawn by Lucide.
LEGACY_ICONS = {
    "home": "house",
    "clipboard": "clipboard-list",
    "people": "users",
    "shield": "shield-check",
    "logout": "log-out",
    "alert": "triangle-alert",
    "box": "package",
    "cart": "shopping-cart",
    "handover": "hand-helping",
}
#: One tone, one icon, everywhere (a chip is never colour alone). Written out in full so
#: Tailwind finds each class name when it scans this file.
CHIP_TONES = {
    "ok": ("chip chip-ok", "circle-check"),
    "warn": ("chip chip-warn", "triangle-alert"),
    "danger": ("chip chip-danger", "circle-x"),
    "brand": ("chip chip-brand", "info"),
    "info": ("chip chip-info", "info"),
    "": ("chip", "circle-dashed"),
}


@register.filter
def at(items: Sequence[Any], index: int) -> Any:
    """``columns|at:forloop.counter0``: the item at ``index``, or an empty string."""
    try:
        return items[index]
    except IndexError, TypeError:
        return ""


@register.filter
def quantity(value: Decimal | str | None) -> str:
    """``10.000`` → ``10``, ``2.500`` → ``2,5`` (in Spanish): a quantity without trailing zeros.

    A string is a stored quantity (the JSON of a revision): ``"2.5"`` reads like ``2.500``.
    """
    if value is None or value == "":
        return ""
    trimmed = Decimal(value).normalize()
    places = len(format(trimmed, "f").partition(".")[2])
    return number_format(trimmed, decimal_pos=places)


@register.simple_tag
def chip(label: object, tone: str = "", icon_name: str = "") -> SafeString:
    """``components/status_chip.html`` as a tag: the same markup without rendering a template
    per chip, for the lists that draw a few of them on every row.

    The tone picks the class and the icon (``icon_name`` overrides the icon): state is an icon
    and a word, never colour alone.
    """
    classes, default_icon = CHIP_TONES.get(tone, CHIP_TONES[""])
    return format_html(
        '<span class="{}">{}{}</span>',
        classes,
        _svg(icon_name or default_icon, "size-3.5", ""),
        label,
    )


@cache
def _icon_shapes() -> dict[str, SafeString]:
    """The inner markup of every vendored icon, built once from escaped attributes."""
    data = json.loads(ICONS_FILE.read_text(encoding="utf-8"))
    return {
        name: format_html_join(
            "",
            "<{} {}/>",
            (
                (tag, format_html_join(" ", '{}="{}"', sorted(attributes.items())))
                for tag, attributes in shapes
            ),
        )
        for name, shapes in data.items()
    }


@lru_cache(maxsize=1024)
def _svg(name: str, classes: str, label: str) -> SafeString:
    """One icon as an inline ``<svg>``: hidden from assistive technology unless it has a label."""
    shapes = _icon_shapes().get(LEGACY_ICONS.get(name, name))
    if shapes is None:
        raise ValueError(f"unknown icon {name!r}: add it with scripts/vendor_icons.py")
    names = " ".join(
        part for part in ("" if "size-" in classes else "size-5", classes, "shrink-0") if part
    )
    accessibility = (
        format_html('role="img" aria-label="{}"', label)
        if label
        else format_html('aria-hidden="{}"', "true")
    )
    return format_html(
        '<svg xmlns="http://www.w3.org/2000/svg" class="{}" viewBox="0 0 24 24" fill="none" '
        'stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" '
        '{} focusable="false">{}</svg>',
        names,
        accessibility,
        shapes,
    )


def icon_names() -> list[str]:
    """Every vendored icon name, for the style guide and the tests."""
    return sorted(_icon_shapes())


@register.simple_tag
def icon(name: str, classes: str = "", label: str = "") -> SafeString:
    """``{% icon "package" %}``: a 24 px outline icon (``classes="size-4"`` to resize).

    Decorative (``aria-hidden``) unless given ``label``. Rows drawn by the hundred cost no
    template include per icon.
    """
    return _svg(name, classes.strip(), label)


@register.filter
def initials(name: object) -> str:
    """``"Elena Navarro"`` → ``EN``: up to two capitals for an avatar."""
    words = str(name or "").replace("@", " ").replace(".", " ").split()
    return "".join(word[0] for word in words[:2]).upper()


@register.simple_tag
def sparkline(values: Sequence[float | Decimal]) -> SafeString:
    """A small trend line for a ``stat_tile``: ``values`` oldest first, drawn in a 84 x 28 box.

    Decorative (the tile states the change in words), so it is hidden from assistive technology.
    Fewer than two values draw nothing.
    """
    numbers = [float(value) for value in values or ()]
    if len(numbers) < SPARK_MIN_POINTS:
        return format_html("{}", "")
    low, high = min(numbers), max(numbers)
    span = (high - low) or 1.0
    step = SPARK_WIDTH / (len(numbers) - 1)
    points = [
        (
            round(index * step, 1),
            round(SPARK_HEIGHT - 2 - (value - low) / span * (SPARK_HEIGHT - 4), 1),
        )
        for index, value in enumerate(numbers)
    ]
    line = " ".join(f"{x},{y}" for x, y in points)
    last_x, last_y = points[-1]
    return format_html(
        '<svg class="spark" viewBox="0 0 {} {}" aria-hidden="true" focusable="false">'
        '<polygon class="spark-area" points="0,{} {} {},{}"/><polyline points="{}"/>'
        '<circle cx="{}" cy="{}" r="2.5"/></svg>',
        SPARK_WIDTH,
        SPARK_HEIGHT,
        SPARK_HEIGHT,
        line,
        SPARK_WIDTH,
        SPARK_HEIGHT,
        line,
        last_x,
        last_y,
    )


@register.simple_tag
def steps(*labels: str) -> list[str]:
    """The labels of a short flow's steps, for ``components/stepper.html``."""
    return list(labels)
