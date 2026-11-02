"""Template helpers of the list toolkit (``apps.core.listing``)."""

from typing import Any
from urllib.parse import urlencode

from django import forms, template

from apps.core import listing
from apps.core.listing import ListPage

register = template.Library()


@register.simple_tag
def active_filter_count(form: forms.Form | None) -> int:
    """How many of the filter form's fields carry a value in the request."""
    return len(listing.active_filters(form)) if form is not None else 0


@register.simple_tag(takes_context=True)
def clear_filters_url(
    context: dict[str, Any], form: forms.Form, listing: ListPage | None = None
) -> str:
    """The list's URL without its filters and page, keeping the page size and the sort.

    Everything else in the query (the other sections of the page) stays; the section's
    ``anchor`` brings the reader back to it.
    """
    request = context["request"]
    filters = {form.add_prefix(name) for name in form.fields}
    filters.add(listing.page_param if listing is not None else "page")
    kept = [(key, value) for key, value in request.GET.items() if key not in filters]
    url = f"{request.path}?{urlencode(kept)}" if kept else request.path
    return f"{url}#{listing.anchor}" if listing is not None and listing.anchor else url


@register.simple_tag(takes_context=True)
def page_url(context: dict[str, Any], listing: ListPage, number: int) -> str:
    """The query string of page ``number`` of ``listing``, keeping every other parameter."""
    query = context["request"].GET.copy()
    query[listing.page_param] = str(number)
    href = f"?{query.urlencode()}"
    return f"{href}#{listing.anchor}" if listing.anchor else href
