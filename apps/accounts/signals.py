"""Receivers connected in ``AccountsConfig.ready``."""

from typing import Any

from django.http import HttpRequest

from apps.accounts.mfa import keep_own_records
from apps.accounts.sessions import bump_session_epoch, keep_current_session, start_session


def on_user_logged_in(sender: Any, request: HttpRequest | None, user: Any, **kwargs: Any) -> None:
    """Every login (allauth, admin, tests' ``force_login``) binds the session to its epoch."""
    if request is None:
        return
    start_session(request, user)
    keep_own_records(request, user)


def on_authenticator_removed(
    sender: Any, request: HttpRequest | None, user: Any, **kwargs: Any
) -> None:
    """Removing a factor ends the user's other sessions; the one that removed it stays."""
    bump_session_epoch(user)
    if request is not None and getattr(request.user, "pk", None) == user.pk:
        keep_current_session(request, user)
