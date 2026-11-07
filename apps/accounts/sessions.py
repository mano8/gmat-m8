"""Session epochs, lifetimes and inactivity (plan §3.6 and §5, "Identity, sessions and MFA").

A session remembers the user's ``session_epoch`` from when it logged in. A password,
email or MFA reset, a deactivation or "sign out everywhere" bumps the epoch, and every
older session ends on its next request (``SessionGuardMiddleware``). Sessions also have an
absolute lifetime that depends on the account: privileged ones (platform operators and
holders of a privileged role, ``apps.access.policy.is_privileged``) are short (Q12).

Inactivity ends a session sooner (M8.3, X6): 30 minutes for a privileged account, 2 hours for
the others, both from the environment. Every request counts, except what a page sends by
itself, such as a status poll (``session_passive``), so a page left open cannot keep a session
alive. The time is written to the session at most once a minute.
"""

import time
from collections.abc import Callable, Mapping
from typing import Any

from allauth.usersessions.models import UserSession
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models import F
from django.http import HttpRequest

from apps.access.policy import is_privileged
from apps.core.http import client_ip

SESSION_EPOCH_KEY = "_gmat_session_epoch"
SESSION_LOGIN_AT_KEY = "_gmat_login_at"
SESSION_SEEN_AT_KEY = "_gmat_seen_at"


def session_limits(user: Any) -> tuple[int, int]:
    """Seconds a session of ``user`` may live after login (Q12) and stay idle (X6)."""
    if is_privileged(user):
        return int(settings.PRIVILEGED_SESSION_AGE), int(settings.PRIVILEGED_SESSION_IDLE)
    return int(settings.SESSION_COOKIE_AGE), int(settings.SESSION_IDLE)


def session_lifetime(user: Any) -> int:
    """Seconds a session of ``user`` may live after login (Q12)."""
    return session_limits(user)[0]


def start_session(request: HttpRequest, user: Any) -> None:
    """Bind a freshly logged-in session to the user's current epoch and login time.

    The epoch is read from the database, not from ``user``: a login can itself bump it
    (Django re-hashes the password when the hasher's parameters changed).
    """
    user.refresh_from_db(fields=["session_epoch"])
    now = int(time.time())
    request.session[SESSION_EPOCH_KEY] = user.session_epoch
    request.session[SESSION_LOGIN_AT_KEY] = now
    request.session[SESSION_SEEN_AT_KEY] = now
    request.session.set_expiry(session_lifetime(user))


def why_stale(
    data: Mapping[str, Any], user: Any, limits: tuple[int, int], now: float
) -> str | None:
    """What ended this session's data, or ``None`` while it is good; ``limits`` from
    ``session_limits``. A session without an epoch or a login time is stale."""
    login_at = data.get(SESSION_LOGIN_AT_KEY)
    if data.get(SESSION_EPOCH_KEY) != user.session_epoch or not isinstance(login_at, int):
        return "epoch"
    lifetime, idle = limits
    if now - login_at >= lifetime:
        return "lifetime"
    if now - data.get(SESSION_SEEN_AT_KEY, login_at) >= idle:
        return "idle"
    return None


def session_stale_reason(request: HttpRequest, user: Any) -> str | None:
    return why_stale(request.session, user, session_limits(user), time.time())


def session_is_current(request: HttpRequest, user: Any) -> bool:
    """False once the epoch moved on, the lifetime ran out or the session stayed idle too long."""
    return session_stale_reason(request, user) is None


def touch_session(request: HttpRequest) -> None:
    """The user did something: note it, and refresh the row of their session list."""
    session = request.session
    now = int(time.time())
    seen_at = session.get(SESSION_SEEN_AT_KEY, session.get(SESSION_LOGIN_AT_KEY, 0))
    if now - seen_at < settings.SESSION_TOUCH_INTERVAL:
        return
    session[SESSION_SEEN_AT_KEY] = now
    if client_ip(request) is not None:  # the list's row needs one; only tests lack it
        UserSession.objects.create_from_request(request)


def session_passive[V: Callable[..., Any]](view: V) -> V:
    """Mark a view as what a page sends by itself: it does not count as activity (X6)."""
    view.session_passive = True  # type: ignore[attr-defined]
    return view


def bump_session_epoch(user: Any) -> None:
    """End every session of ``user`` (an atomic increment; ``user`` is refreshed)."""
    get_user_model().objects.filter(pk=user.pk).update(session_epoch=F("session_epoch") + 1)
    user.refresh_from_db(fields=["session_epoch"])


def keep_current_session(request: HttpRequest, user: Any) -> None:
    """After the user changed their own security settings, keep only this session alive."""
    request.session[SESSION_EPOCH_KEY] = user.session_epoch
