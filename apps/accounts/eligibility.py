"""When the worker may still send an account's security emails (plan §5, eligibility).

Registered with ``apps.notifications.eligibility`` from ``AccountsConfig.ready``. Each rule is
evaluated in the claim transaction, against the token row the message is about:

- ``invitation.pending``: the invitation is neither accepted nor revoked, and not expired;
- ``email_change.confirmable``: the change is open, unconfirmed and not expired;
- ``email_change.cancellable``: the change is still open (the old address may cancel until
  it is applied).
"""

from apps.accounts.models import EmailChangeRequest, Invitation
from apps.accounts.services import (
    EMAIL_CHANGE_CANCELLABLE,
    EMAIL_CHANGE_CONFIRMABLE,
    INVITATION_PENDING,
)
from apps.core.db import db_now
from apps.notifications.eligibility import register_security_rule
from apps.notifications.models import EmailOutbox


def _target[M: (Invitation, EmailChangeRequest)](message: EmailOutbox, model: type[M]) -> M | None:
    if (
        message.eligibility_object_type != model._meta.label_lower
        or message.eligibility_object_id is None
    ):
        return None
    return model.objects.filter(pk=message.eligibility_object_id).first()


def invitation_pending(message: EmailOutbox) -> bool:
    invitation = _target(message, Invitation)
    return (
        invitation is not None
        and invitation.is_pending
        and invitation.expires_at > db_now()
        and invitation.email == message.recipient_address.lower()
    )


def email_change_confirmable(message: EmailOutbox) -> bool:
    change = _target(message, EmailChangeRequest)
    return (
        change is not None
        and change.is_open
        and change.confirmed_at is None
        and change.expires_at > db_now()
        and change.new_email == message.recipient_address.lower()
    )


def email_change_cancellable(message: EmailOutbox) -> bool:
    change = _target(message, EmailChangeRequest)
    return change is not None and change.is_open


def register() -> None:
    register_security_rule(INVITATION_PENDING, invitation_pending)
    register_security_rule(EMAIL_CHANGE_CONFIRMABLE, email_change_confirmable)
    register_security_rule(EMAIL_CHANGE_CANCELLABLE, email_change_cancellable)
