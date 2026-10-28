"""Template context shared by every page of the UI shell."""

from django.http import HttpRequest

from config.logging import TOKEN_PATH_PREFIXES


def ui(request: HttpRequest) -> dict[str, str]:
    """Where the language switcher returns to.

    The current path, except on token-bearing pages: a path with a secret in it must not be
    copied into another form or redirect, so those go back to the home page.
    """
    path = request.path
    if any(prefix in path for prefix in TOKEN_PATH_PREFIXES):
        path = "/"
    return {"language_next": path}
