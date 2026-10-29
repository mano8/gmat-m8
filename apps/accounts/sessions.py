"""Session epochs and lifetimes (plan §3.6 and §5, "Identity, sessions and MFA").

A session remembers the user's ``session_epoch`` from when it logged in. A password,
email or MFA reset, a deactivation or "sign out everywhere" bumps the epoch, and every
older session ends on its next request (``SessionGuardMiddleware``). Sessions also have an
absolute lifetime that depends on the account: privileged ones are short (Q12).
"""

import time
from typing import Any

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models import F
from django.http import HttpRequest

SESSION_EPOCH_KEY = "_gmat_session_epoch"
SESSION_LOGIN_AT_KEY = "_gmat_login_at"


def is_privileged(user: Any) -> bool:
    """Accounts with short sessions and mandatory MFA.

    Today these are the platform operators (staff and superusers). M2.3 adds the
    privileged roles (``GROUP_ADMIN``, ``MATERIAL_MANAGER``).
    """
    return bool(user.is_staff or user.is_superuser)


def session_lifetime(user: Any) -> int:
    """Seconds a session of ``user`` may live after login (Q12)."""
    if is_privileged(user):
        return int(settings.PRIVILEGED_SESSION_AGE)
    return int(settings.SESSION_COOKIE_AGE)


def start_session(request: HttpRequest, user: Any) -> None:
    """Bind a freshly logged-in session to the user's current epoch and login time.

    The epoch is read from the database, not from ``user``: a login can itself bump it
    (Django re-hashes the password when the hasher's parameters changed).
    """
    user.refresh_from_db(fields=["session_epoch"])
    request.session[SESSION_EPOCH_KEY] = user.session_epoch
    request.session[SESSION_LOGIN_AT_KEY] = int(time.time())
    request.session.set_expiry(session_lifetime(user))


def session_is_current(request: HttpRequest, user: Any) -> bool:
    """False once the epoch moved on or the lifetime ran out; a session without them fails."""
    epoch = request.session.get(SESSION_EPOCH_KEY)
    login_at = request.session.get(SESSION_LOGIN_AT_KEY)
    if epoch != user.session_epoch or not isinstance(login_at, int):
        return False
    return time.time() - login_at < session_lifetime(user)


def bump_session_epoch(user: Any) -> None:
    """End every session of ``user`` (an atomic increment; ``user`` is refreshed)."""
    get_user_model().objects.filter(pk=user.pk).update(session_epoch=F("session_epoch") + 1)
    user.refresh_from_db(fields=["session_epoch"])


def keep_current_session(request: HttpRequest, user: Any) -> None:
    """After the user changed their own security settings, keep only this session alive."""
    request.session[SESSION_EPOCH_KEY] = user.session_epoch
