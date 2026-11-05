"""Receivers connected in ``AccountsConfig.ready``.

Credential changes also go to the audit trail: they are what a restore must not undo, and
``post_restore_reconcile`` replays them from a preserved audit log (plan §9, step 5). The
security log has them too, but a container's log does not outlive it.
"""

from typing import Any

from django.contrib import messages
from django.db import transaction
from django.http import HttpRequest
from django.utils.translation import gettext as _

from apps.accounts.mfa import keep_own_records, settle_recovery_codes
from apps.accounts.sessions import bump_session_epoch, keep_current_session, start_session
from apps.audit.recorder import AuditIdentity, record
from apps.core.http import client_ip

VIA_SELF_SERVICE = "allauth"


def _audit_credential(action: str, request: HttpRequest | None, user: Any, **data: str) -> None:
    identity = AuditIdentity(
        label=user.email,
        user_id=user.pk,
        via=VIA_SELF_SERVICE,
        ip=client_ip(request) if request is not None else None,
    )
    with transaction.atomic():
        record(identity, action, obj=user, data=data)


def on_user_logged_in(sender: Any, request: HttpRequest | None, user: Any, **kwargs: Any) -> None:
    """Every login (allauth, admin, tests' ``force_login``) binds the session to its epoch."""
    if request is None:
        return
    start_session(request, user)
    keep_own_records(request, user)
    if user.recovery_codes_owed:
        messages.warning(
            request,
            _("Your recovery codes stopped working after a restore. Generate a new set."),
            fail_silently=True,
        )


def on_authenticator_removed(
    sender: Any, request: HttpRequest | None, user: Any, **kwargs: Any
) -> None:
    """Removing a factor ends the user's other sessions; the one that removed it stays."""
    bump_session_epoch(user)
    if request is not None and getattr(request.user, "pk", None) == user.pk:
        keep_current_session(request, user)
    factor = str(getattr(kwargs.get("authenticator"), "type", "") or "")
    _audit_credential("user.authenticator_removed", request, user, type=factor)


def on_recovery_codes_changed(
    sender: Any, request: HttpRequest | None, user: Any, **kwargs: Any
) -> None:
    """A factor added or codes generated: an account that owed a new set may now have it."""
    settle_recovery_codes(user)


def on_password_changed(sender: Any, request: HttpRequest | None, user: Any, **kwargs: Any) -> None:
    _audit_credential("user.password_changed", request, user, how="changed")


def on_password_set(sender: Any, request: HttpRequest | None, user: Any, **kwargs: Any) -> None:
    _audit_credential("user.password_changed", request, user, how="set")


def on_password_reset(sender: Any, request: HttpRequest | None, user: Any, **kwargs: Any) -> None:
    _audit_credential("user.password_changed", request, user, how="reset")
