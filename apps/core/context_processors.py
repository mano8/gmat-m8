"""Template context shared by every page of the UI shell."""

from django.conf import settings
from django.http import HttpRequest

from apps.core.preferences import Preferences, parse
from config.logging import TOKEN_PATH_PREFIXES


def ui(request: HttpRequest) -> dict[str, str]:
    """Where the language switcher and the display preferences return to.

    The current path (never its query string), except on token-bearing pages: a path with a
    secret in it must not be copied into another form or redirect, so those go back to the
    home page.
    """
    path = request.path
    if any(prefix in path for prefix in TOKEN_PATH_PREFIXES):
        path = "/"
    return {"language_next": path}


def preferences(request: HttpRequest) -> dict[str, object]:
    """The display preferences as constants (M8b.2, Q69).

    The cookie's text is parsed against a fixed grammar and never reaches a template: pages print
    only these three values.
    """
    prefs: Preferences = parse(request.COOKIES.get(settings.UI_COOKIE_NAME))
    return {
        "ui_theme": prefs.theme,
        "ui_menu_hidden": prefs.menu_hidden,
        "ui_folded": prefs.folded,
    }
