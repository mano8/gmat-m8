"""Template helpers of the UI shell."""

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from django import template
from django.utils.formats import number_format
from django.utils.html import format_html
from django.utils.safestring import SafeString

register = template.Library()


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
def chip(label: object, tone: str = "") -> SafeString:
    """``components/status_chip.html`` as a tag: the same markup without rendering a template
    per chip, for the lists that draw a few of them on every row."""
    return format_html('<span class="chip{}">{}</span>', f" chip-{tone}" if tone else "", label)
