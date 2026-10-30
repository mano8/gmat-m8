"""Queueing email: the only way application code sends mail (plan §5).

The helpers write ``EmailOutbox`` rows in the caller's transaction, so a message exists
exactly when the change that causes it commits, and its content is frozen there. They
authorize nothing themselves: the service that calls them has already run
``authorize_in_txn`` for its own operation (as for ``audit.record``). Only the worker sends.

- ``queue_notification``: recipients as user ids plus an eligibility rule (a capability on
  an object), split into messages of at most ``MAX_RECIPIENTS`` BCC recipients;
- ``queue_security_email``: one address, a named rule on its target, the bodies sealed;
  expires after ``OUTBOX_SENSITIVE_TTL``. allauth's mail arrives here through
  ``apps.notifications.backends.OutboxEmailBackend``;
- ``queue_dispatch``: a supplier dispatch of a purchase order (M5.3).
"""

from collections.abc import Iterable
from datetime import timedelta
from uuid import UUID

from django.conf import settings
from django.db import DEFAULT_DB_ALIAS, connections
from django.db.models import Model

from apps.access.roles import Capability
from apps.core.db import db_now
from apps.notifications import eligibility
from apps.notifications.crypto import seal
from apps.notifications.models import DISPATCH_KINDS, MAX_RECIPIENTS, EmailOutbox, Kind


class OutboxOutsideTransaction(RuntimeError):
    """A business email was queued outside the transaction of the change that causes it."""


def _require_transaction(using: str) -> None:
    if not connections[using].in_atomic_block:
        raise OutboxOutsideTransaction("Queue business email inside the service transaction")


def _from(from_email: str | None) -> str:
    return from_email or settings.DEFAULT_FROM_EMAIL


def queue_notification(
    *,
    recipients: Iterable[UUID],
    capability: str,
    obj: Model | None,
    subject: str,
    body: str,
    html_body: str = "",
    source_ref: str = "",
    from_email: str | None = None,
    using: str = DEFAULT_DB_ALIAS,
) -> list[EmailOutbox]:
    """Queue one notification per ``MAX_RECIPIENTS`` recipients; return the messages.

    Each recipient must still be active and hold ``capability`` on ``obj`` when the worker
    claims the message; ``obj`` must be a scoped model (it has ``visible_to``).
    """
    _require_transaction(using)
    if capability not in set(Capability):
        raise ValueError(f"Unknown capability {capability!r}")
    object_type, object_id = "", None
    if obj is not None:
        if not eligibility.is_scoped(type(obj)):
            raise TypeError(f"{type(obj).__name__} has no scoped queryset (visible_to)")
        object_type, object_id = eligibility.object_reference(obj)
    user_ids = sorted(set(recipients))
    return [
        EmailOutbox.objects.using(using).create(
            kind=Kind.NOTIFICATION,
            recipient_users=user_ids[start : start + MAX_RECIPIENTS],
            eligibility_rule=capability,
            eligibility_object_type=object_type,
            eligibility_object_id=object_id,
            source_ref=source_ref,
            from_email=_from(from_email),
            subject=subject,
            body=body,
            html_body=html_body,
        )
        for start in range(0, len(user_ids), MAX_RECIPIENTS)
    ]


def queue_security_email(
    *,
    address: str,
    target: Model,
    rule: str,
    subject: str,
    body: str,
    html_body: str = "",
    source_ref: str = "",
    from_email: str | None = None,
    using: str = DEFAULT_DB_ALIAS,
) -> EmailOutbox:
    """Queue a sensitive message to ``address``, sent only while ``rule`` holds for ``target``.

    May run outside a transaction (allauth sends from its own views); inside one, the
    message commits with it.
    """
    if not eligibility.is_registered(rule):
        raise ValueError(f"Unknown security rule {rule!r}")
    object_type, object_id = eligibility.object_reference(target)
    ttl = timedelta(seconds=int(settings.OUTBOX_SENSITIVE_TTL))
    return EmailOutbox.objects.using(using).create(
        kind=Kind.SECURITY,
        sensitive=True,
        recipient_address=address,
        eligibility_rule=rule,
        eligibility_object_type=object_type,
        eligibility_object_id=object_id,
        source_ref=source_ref,
        from_email=_from(from_email),
        subject=subject,
        body=seal(body),
        html_body=seal(html_body),
        expires_at=db_now(using=using) + ttl,
    )


def queue_dispatch(
    *,
    kind: Kind,
    po_id: UUID,
    address: str,
    subject: str,
    body: str,
    html_body: str = "",
    source_ref: str = "",
    from_email: str | None = None,
    using: str = DEFAULT_DB_ALIAS,
) -> EmailOutbox:
    """Queue a supplier dispatch of purchase order ``po_id``, under its L4 lock (M5.3)."""
    _require_transaction(using)
    if kind not in DISPATCH_KINDS:
        raise ValueError(f"{kind} is not a dispatch kind")
    return EmailOutbox.objects.using(using).create(
        kind=kind,
        recipient_address=address,
        source_po=po_id,
        source_ref=source_ref,
        from_email=_from(from_email),
        subject=subject,
        body=body,
        html_body=html_body,
    )
