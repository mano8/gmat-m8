"""``{% bell %}``: the header's bell with the person's unread count (M17.4, Q99).

    {% load notice_tags %}
    {% bell %}

Drawn for who may read their notifications (every current member), with the count loaded with
the page; ``notifications/bell.html`` then reloads itself every minute. It shows a count only,
which the selector works out from the person's own rows they may still see.
"""

from typing import Any

from django import template

from apps.access.middleware import access_for
from apps.access.roles import Capability
from apps.notifications import selectors

register = template.Library()


@register.inclusion_tag("notifications/bell.html", takes_context=True)
def bell(context: template.Context) -> dict[str, Any]:
    request = context.get("request")
    ctx = access_for(request) if request is not None else None
    if ctx is None or not ctx.has(Capability.CATALOG_VIEW):
        return {"unread": None}
    return {"unread": selectors.unread_count(ctx)}
