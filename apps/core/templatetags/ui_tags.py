"""Template helpers of the UI shell."""

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from django import template
from django.utils.formats import number_format

register = template.Library()


@register.filter
def at(items: Sequence[Any], index: int) -> Any:
    """``columns|at:forloop.counter0``: the item at ``index``, or an empty string."""
    try:
        return items[index]
    except IndexError, TypeError:
        return ""


@register.filter
def quantity(value: Decimal | None) -> str:
    """``10.000`` → ``10``, ``2.500`` → ``2,5`` (in Spanish): a quantity without trailing zeros."""
    if value is None:
        return ""
    trimmed = value.normalize()
    exponent = trimmed.as_tuple().exponent
    places = max(0, -exponent) if isinstance(exponent, int) else 0
    return number_format(trimmed, decimal_pos=places)
