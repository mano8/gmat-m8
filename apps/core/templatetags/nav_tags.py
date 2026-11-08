"""``{% app_navigation as nav %}``: the signed-in menu, built once per page (M8b.2)."""

from django import template

from apps.core.navigation import Navigation, navigation

register = template.Library()


@register.simple_tag(takes_context=True)
def app_navigation(context: template.Context) -> Navigation:
    """The menu for this request: categories, the phone bar, the More sheet, the current page.

    The folded categories come from the preferences context processor (constants parsed from the
    cookie), never from the cookie's text.
    """
    folded: frozenset[str] = context.get("ui_folded") or frozenset()
    return navigation(context["request"], folded)
