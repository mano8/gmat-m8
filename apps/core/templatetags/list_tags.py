"""Template helpers of the list toolkit (``apps.core.listing``)."""

from typing import Any
from urllib.parse import urlencode

from django import forms, template

from apps.core import listing

register = template.Library()


@register.simple_tag
def active_filter_count(form: forms.Form | None) -> int:
    """How many of the filter form's fields carry a value in the request."""
    return len(listing.active_filters(form)) if form is not None else 0


@register.simple_tag(takes_context=True)
def clear_filters_url(context: dict[str, Any], form: forms.Form) -> str:
    """The list's URL without its filters and page, keeping the page size and the sort."""
    request = context["request"]
    filters = set(form.fields) | {"page"}
    kept = [(key, value) for key, value in request.GET.items() if key not in filters]
    return f"{request.path}?{urlencode(kept)}" if kept else request.path
