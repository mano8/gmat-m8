"""Admin-initiated email changes: the new address confirms, the old one can cancel (Q28)."""

from uuid import UUID

from django.core.exceptions import ValidationError
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

from apps.access.authorization import authorize_in_txn
from apps.access.principals import Principal, TokenPrincipal
from apps.accounts import tokens
from apps.accounts.models import EmailChangeRequest, User
from apps.accounts.services.common import (
    EMAIL_CHANGE_CANCELLABLE,
    EMAIL_CHANGE_CONFIRM_MAIL,
    EMAIL_CHANGE_CONFIRMABLE,
    EMAIL_CHANGE_NOTICE_MAIL,
    REASON_REPLACED,
    _absolute,
    _hours,
    _lock_and_authorize,
    _normalize_email,
    _subject,
    _token_row,
)
from apps.audit.recorder import record
from apps.core import events
from apps.core.db import db_now
from apps.core.locking import LockOrder, transactional
from apps.notifications.outbox import expire_security_email, queue_security_email


def _queue_email_change_emails(
    change: EmailChangeRequest, confirm_token: str, cancel_token: str
) -> None:
    context = {
        "change": change,
        "hours": _hours(tokens.EMAIL_CHANGE_LIFETIME),
        "cooling_off_hours": _hours(tokens.EMAIL_CHANGE_COOLING_OFF),
    }
    queue_security_email(
        address=change.new_email,
        target=change,
        rule=EMAIL_CHANGE_CONFIRMABLE,
        subject=_subject(gettext("Confirm your new email address")),
        body=render_to_string(
            "accounts/email/email_change_confirm.txt",
            {**context, "link": _absolute(reverse("email_change:link", args=[confirm_token]))},
        ),
        source_ref=EMAIL_CHANGE_CONFIRM_MAIL,
    )
    queue_security_email(
        address=change.user.email,
        target=change,
        rule=EMAIL_CHANGE_CANCELLABLE,
        subject=_subject(gettext("Your email address is about to change")),
        body=render_to_string(
            "accounts/email/email_change_notice.txt",
            {**context, "link": _absolute(reverse("email_change:link", args=[cancel_token]))},
        ),
        source_ref=EMAIL_CHANGE_NOTICE_MAIL,
    )


@transactional
def request_email_change(
    principal: Principal, user_id: UUID, *, new_email: str
) -> EmailChangeRequest:
    """Start an email change: the new address confirms, the old one may cancel (Q26, Q28).

    A previous open request of the account is replaced.
    """
    actor = _lock_and_authorize(principal, user_id)
    user = User.objects.get(pk=user_id)
    new_email = _normalize_email(new_email)
    if not user.is_active or user.offboarded_at is not None:
        raise ValidationError(_("This account is inactive."), code="inactive")
    if new_email == user.email:
        raise ValidationError(
            {"new_email": ValidationError(_("This is the current address."), code="same")}
        )
    if User.objects.filter(email=new_email).exists():
        raise ValidationError(
            {"new_email": ValidationError(_("Another account uses this address."), code="taken")}
        )
    previous = list(
        EmailChangeRequest.objects.select_for_update().filter(
            user=user, applied_at__isnull=True, cancelled_at__isnull=True, revoked_at__isnull=True
        )
    )
    now = db_now()
    for item in previous:
        item.revoked_at = now
        item.revoke_reason = REASON_REPLACED
        item.save(update_fields=["revoked_at", "revoke_reason", "updated_at"])
    expire_security_email(targets=previous)
    confirm_raw, confirm_hash = tokens.new_token()
    cancel_raw, cancel_hash = tokens.new_token()
    change = EmailChangeRequest(
        user=user,
        new_email=new_email,
        requested_by=actor,
        token_hash=confirm_hash,
        cancel_token_hash=cancel_hash,
        expires_at=now + tokens.EMAIL_CHANGE_LIFETIME,
    )
    change.full_clean()
    change.save()
    _queue_email_change_emails(change, confirm_raw, cancel_raw)
    record(
        principal.audit_identity,
        "email_change.requested",
        obj=change,
        data={
            "user_id": user.pk,
            "new_email": new_email,
            "replaces": [item.pk for item in previous],
        },
    )
    events.publish("email_change.requested", change_id=change.pk, user_id=user.pk)
    return change


@transactional
def revoke_email_change(
    principal: Principal, change_id: UUID, *, reason: str = ""
) -> EmailChangeRequest:
    user_id = EmailChangeRequest.objects.values_list("user_id", flat=True).get(pk=change_id)
    _lock_and_authorize(principal, user_id)
    change = EmailChangeRequest.objects.select_for_update().get(pk=change_id)
    if not change.is_open:
        raise ValidationError(_("This email change is no longer open."), code="closed")
    change.revoked_at = db_now()
    change.revoke_reason = reason.strip()[:200]
    change.save(update_fields=["revoked_at", "revoke_reason", "updated_at"])
    expire_security_email(targets=[change])
    record(
        principal.audit_identity,
        "email_change.revoked",
        obj=change,
        data={"reason": change.revoke_reason},
    )
    events.publish("email_change.revoked", change_id=change.pk)
    return change


@transactional
def confirm_email_change(principal: TokenPrincipal) -> EmailChangeRequest:
    """The new address confirms; the cooling-off starts (the worker applies it later)."""
    authorize_in_txn(principal, tokens.CONFIRM_EMAIL_CHANGE, locks=LockOrder())
    change = EmailChangeRequest.objects.get(pk=_token_row(principal))
    now = db_now()
    change.confirmed_at = now
    change.apply_after = now + tokens.EMAIL_CHANGE_COOLING_OFF
    change.save(update_fields=["confirmed_at", "apply_after", "updated_at"])
    record(
        principal.audit_identity,
        "email_change.confirmed",
        obj=change,
        data={"apply_after": change.apply_after},
    )
    events.publish("email_change.confirmed", change_id=change.pk)
    return change


@transactional
def cancel_email_change(principal: TokenPrincipal) -> EmailChangeRequest:
    """The old address cancels, any time before the change is applied."""
    authorize_in_txn(principal, tokens.CANCEL_EMAIL_CHANGE, locks=LockOrder())
    change = EmailChangeRequest.objects.get(pk=_token_row(principal))
    change.cancelled_at = db_now()
    change.save(update_fields=["cancelled_at", "updated_at"])
    expire_security_email(targets=[change])
    record(principal.audit_identity, "email_change.cancelled", obj=change)
    events.publish("email_change.cancelled", change_id=change.pk)
    return change
