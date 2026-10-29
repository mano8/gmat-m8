"""Template helpers of the UI shell."""

from collections.abc import Sequence
from typing import Any

from django import template

register = template.Library()


@register.filter
def at(items: Sequence[Any], index: int) -> Any:
    """``columns|at:forloop.counter0``: the item at ``index``, or an empty string."""
    try:
        return items[index]
    except IndexError, TypeError:
        return ""
