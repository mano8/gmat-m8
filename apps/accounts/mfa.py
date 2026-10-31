"""Session-level MFA (plan §3.6 and §5).

"MFA required" means that *this session* completed MFA after the user's last privilege
elevation. Having an enrolled authenticator is not enough. The check reads allauth's
authentication records for the session and only counts an MFA record whose authenticator
belongs to the logged-in user, so records left over from another account's abandoned login
can never satisfy it.
"""

from typing import Any
from urllib.parse import urlencode

from allauth.account.authentication import get_authentication_records
from allauth.account.internal.flows.login import AUTHENTICATION_METHODS_SESSION_KEY
from allauth.mfa.models import Authenticator
from allauth.mfa.utils import is_mfa_enabled
from django.contrib.auth import get_user_model
from django.contrib.auth.base_user import AbstractBaseUser
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.urls import reverse

from apps.access.policy import is_privileged, mfa_satisfied
from apps.accounts.sessions import bump_session_epoch
from apps.core.db import atomic_with_lock_timeout

# Reachable without session MFA, so that a privileged user can complete it, sign out or
# change the language: allauth's account and MFA pages, the language switch, health checks.
MFA_EXEMPT_URL_NAMES = frozenset({"set_language", "healthz", "readyz"})
MFA_EXEMPT_URL_PREFIXES = ("account_", "mfa_")
# The token links only store the token's hash and redirect to a token-less page: a step-up
# from here would carry the token into the ``next`` parameter.
MFA_EXEMPT_VIEW_NAMES = frozenset({"invitations:link", "email_change:link"})


def _mfa_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        record
        for record in records
        if record.get("method") == "mfa"
        and isinstance(record.get("id"), int)
        and isinstance(record.get("at"), int | float)
    ]


def _owned_authenticator_ids(user: Any, records: list[dict[str, Any]]) -> set[int]:
    ids = {record["id"] for record in _mfa_records(records)}
    if not ids:
        return set()
    return set(
        Authenticator.objects.filter(user_id=user.pk, pk__in=ids).values_list("pk", flat=True)
    )


def session_mfa_at(request: HttpRequest) -> float | None:
    """When this session last completed MFA with one of the user's own authenticators."""
    user = request.user
    if not user.is_authenticated:
        return None
    records = list(get_authentication_records(request))
    owned = _owned_authenticator_ids(user, records)
    times = [float(record["at"]) for record in _mfa_records(records) if record["id"] in owned]
    return max(times, default=None)


def session_passed_mfa(request: HttpRequest) -> bool:
    """True if this session completed MFA after the user's last privilege elevation."""
    elevated = getattr(request.user, "privilege_elevated_at", None)
    return mfa_satisfied(session_mfa_at(request), elevated)


def keep_own_records(request: HttpRequest, user: Any) -> None:
    """Drop authentication records that do not belong to ``user`` (called at login).

    Password records must name the user's email; MFA records must use one of the user's
    authenticators. Anything else came from another login attempt and is discarded.
    """
    records = list(get_authentication_records(request))
    if not records:
        return
    owned = _owned_authenticator_ids(user, records)
    email = (user.email or "").lower()
    kept = [
        record
        for record in records
        if (record.get("method") == "password" and str(record.get("email", "")).lower() == email)
        or (record.get("method") == "mfa" and record.get("id") in owned)
    ]
    request.session[AUTHENTICATION_METHODS_SESSION_KEY] = kept


def mfa_required_for(request: HttpRequest) -> bool:
    """The step-up hook: does this request need a session that passed MFA?

    Every admin URL does. For a privileged user (platform operator or holder of a
    privileged role) every other URL does too, except the exempt ones above; a session
    that completed MFA before the user's last privilege elevation steps up again.
    """
    match = request.resolver_match
    if match is None:
        return False
    if "admin" in match.app_names:
        return match.url_name not in {"login", "logout"}
    name = match.url_name or ""
    if match.view_name in MFA_EXEMPT_VIEW_NAMES:
        return False
    if name in MFA_EXEMPT_URL_NAMES or name.startswith(MFA_EXEMPT_URL_PREFIXES):
        return False
    return is_privileged(request.user)


def step_up_response(request: HttpRequest) -> HttpResponse:
    """Send the session to MFA: reauthenticate if enrolled, enroll otherwise.

    Only safe methods are redirected; a write without session MFA is refused (403).
    """
    if request.method not in {"GET", "HEAD"}:
        raise PermissionDenied("This action needs a session that completed MFA")
    user = request.user
    enrolled = isinstance(user, AbstractBaseUser) and is_mfa_enabled(user)
    target = "mfa_reauthenticate" if enrolled else "mfa_index"
    query = urlencode({"next": request.get_full_path()})
    return HttpResponseRedirect(f"{reverse(target)}?{query}")


def reset_mfa(user: Any) -> None:
    """Remove every authenticator of ``user`` and end all of their sessions.

    An internal primitive: the administrator service (M2.6) calls it under its own
    authorization and audit, as offboarding and re-hire do.
    """
    with atomic_with_lock_timeout():
        locked = get_user_model().objects.select_for_update().get(pk=user.pk)
        Authenticator.objects.filter(user_id=locked.pk).delete()
        bump_session_epoch(locked)
    user.refresh_from_db(fields=["session_epoch"])
