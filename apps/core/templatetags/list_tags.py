"""Template helpers of the list toolkit (``apps.core.listing``)."""

from typing import Any
from urllib.parse import urlencode

from django import forms, template

from apps.core import listing
from apps.core.listing import CURSOR_START, CursorPage, ListPage

register = template.Library()


@register.simple_tag
def active_filter_count(form: forms.Form | None) -> int:
    """How many of the filter form's fields carry a value in the request."""
    return len(listing.active_filters(form)) if form is not None else 0


@register.simple_tag(takes_context=True)
def clear_filters_url(
    context: dict[str, Any], form: forms.Form, listing: ListPage | CursorPage | None = None
) -> str:
    """The list's URL without its filters and page, keeping the page size and the sort.

    Everything else in the query (the other sections of the page) stays; the section's
    ``anchor`` brings the reader back to it.
    """
    request = context["request"]
    filters = {form.add_prefix(name) for name in form.fields}
    filters.update(listing.reset_params if listing is not None else ("page",))
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


@register.simple_tag(takes_context=True)
def cursor_url(
    context: dict[str, Any], listing: CursorPage, after: str, back: tuple[str, ...] = ()
) -> str:
    """The query string of the page of ``listing`` that starts at ``after``, remembering the
    pages ``back``; every other parameter stays. The first page carries neither."""
    query = context["request"].GET.copy()
    for name in (listing.scope.after_param, listing.scope.back_param):
        query.pop(name, None)
    if after != CURSOR_START:
        query[listing.scope.after_param] = after
    if back:
        query[listing.scope.back_param] = ",".join(back)
    href = f"?{query.urlencode()}" if query else "?"
    return f"{href}#{listing.anchor}" if listing.anchor else href


@register.simple_tag(takes_context=True)
def cursor_previous(context: dict[str, Any], listing: CursorPage) -> str:
    """The link to the page before this one."""
    after, back = listing.previous
    return cursor_url(context, listing, after, back)


@register.simple_tag(takes_context=True)
def cursor_next(context: dict[str, Any], listing: CursorPage) -> str:
    """The link to the page after this one, which remembers this page and the ones before."""
    return cursor_url(context, listing, listing.next, listing.following)
