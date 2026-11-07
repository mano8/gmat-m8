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
- ``waiting`` and ``defer``: a notice that covers every change until it is sent (the
  reviewer edits of a request, M7b.3) is queued once, held back for a while, and later
  changes find it waiting instead of queueing another;
- ``queue_dispatch``: a supplier dispatch of a purchase order (M5.3);
- ``expire_security_email``: offboarding expires the unsent security emails of an account
  and of the tokens it revokes (plan §5).
"""

from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime, timedelta
from email.utils import parseaddr
from uuid import UUID

from django.conf import settings
from django.db import DEFAULT_DB_ALIAS, connections
from django.db.models import Model, Q
from django.db.models.functions import Now

from apps.access.roles import Capability
from apps.core.db import db_now
from apps.notifications import eligibility
from apps.notifications.crypto import seal
from apps.notifications.models import DISPATCH_KINDS, MAX_RECIPIENTS, EmailOutbox, Kind, Status


class OutboxOutsideTransaction(RuntimeError):
    """A business email was queued outside the transaction of the change that causes it."""


def _require_transaction(using: str) -> None:
    if not connections[using].in_atomic_block:
        raise OutboxOutsideTransaction("Queue business email inside the service transaction")


def _from(from_email: str | None) -> str:
    return from_email or settings.DEFAULT_FROM_EMAIL


def sender_domain(from_email: str) -> str | None:
    """The domain of a sender address (for ``Message-ID`` headers); ``None`` without one."""
    _, at, domain = parseaddr(from_email)[1].rpartition("@")
    return domain if at and domain else None


def queue_notification(
    *,
    recipients: Iterable[UUID],
    capability: str,
    obj: Model | None,
    subject: str,
    body: str,
    html_body: str = "",
    source_ref: str = "",
) -> list[EmailOutbox]:
    """Queue one notification per ``MAX_RECIPIENTS`` recipients; return the messages.

    Each recipient must still be active and hold ``capability`` on ``obj`` when the worker
    claims the message; ``obj`` must be a scoped model (it has ``visible_to``).
    """
    _require_transaction(DEFAULT_DB_ALIAS)
    if capability not in set(Capability):
        raise ValueError(f"Unknown capability {capability!r}")
    object_type, object_id = "", None
    if obj is not None:
        if not eligibility.is_scoped(type(obj)):
            raise TypeError(f"{type(obj).__name__} has no scoped queryset (visible_to)")
        object_type, object_id = eligibility.object_reference(obj)
    user_ids = sorted(set(recipients))
    return [
        EmailOutbox.objects.create(
            kind=Kind.NOTIFICATION,
            recipient_users=user_ids[start : start + MAX_RECIPIENTS],
            eligibility_rule=capability,
            eligibility_object_type=object_type,
            eligibility_object_id=object_id,
            source_ref=source_ref,
            from_email=_from(None),
            subject=subject,
            body=body,
            html_body=html_body,
        )
        for start in range(0, len(user_ids), MAX_RECIPIENTS)
    ]


def waiting(source_ref: str) -> bool:
    """Whether a notification with ``source_ref`` waits in the queue, unclaimed.

    The row is locked ``FOR UPDATE SKIP LOCKED`` until the caller's transaction ends, so the
    worker cannot send it before the change the caller is making commits. One the worker is
    claiming at this moment is skipped (the claim also skips locked rows, so neither side
    ever waits): the caller then queues a new one.
    """
    _require_transaction(DEFAULT_DB_ALIAS)
    found = (
        EmailOutbox.objects.filter(
            kind=Kind.NOTIFICATION, status=Status.QUEUED, source_ref=source_ref
        )
        .select_for_update(skip_locked=True)
        .first()
    )
    return found is not None


def defer(messages: Iterable[EmailOutbox], until: datetime) -> None:
    """Hold these queued messages back until ``until`` (inside the queueing transaction)."""
    _require_transaction(DEFAULT_DB_ALIAS)
    EmailOutbox.objects.filter(
        pk__in=[message.pk for message in messages], status=Status.QUEUED
    ).update(not_before=until)


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
) -> EmailOutbox:
    """Queue a sensitive message to ``address``, sent only while ``rule`` holds for ``target``.

    May run outside a transaction (allauth sends from its own views); inside one, the
    message commits with it.
    """
    if not eligibility.is_registered(rule):
        raise ValueError(f"Unknown security rule {rule!r}")
    object_type, object_id = eligibility.object_reference(target)
    ttl = timedelta(seconds=int(settings.OUTBOX_SENSITIVE_TTL))
    return EmailOutbox.objects.create(
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
        expires_at=db_now() + ttl,
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
    message_id: str = "",
) -> EmailOutbox:
    """Queue a supplier dispatch of purchase order ``po_id``, under its L4 lock (M5.3).

    ``message_id`` is the ``Message-ID`` header, stable per revision so the supplier can
    deduplicate; without one the worker generates it at the first claim. Dispatches come
    from ``DEFAULT_FROM_EMAIL`` and live in the default database.
    """
    _require_transaction(DEFAULT_DB_ALIAS)
    if kind not in DISPATCH_KINDS:
        raise ValueError(f"{kind} is not a dispatch kind")
    return EmailOutbox.objects.create(
        kind=kind,
        recipient_address=address,
        source_po_id=po_id,
        source_ref=source_ref,
        message_id=message_id,
        from_email=_from(None),
        subject=subject,
        body=body,
        html_body=html_body,
    )


def expire_security_email(
    *,
    addresses: Iterable[str] = (),
    targets: Iterable[Model] = (),
    using: str = DEFAULT_DB_ALIAS,
) -> int:
    """Expire and wipe the queued security emails to ``addresses`` or about ``targets``.

    A message the worker is sending right now is left to its outcome; the token or account
    it is about no longer passes its rule, so a retry is suppressed at the next claim.
    """
    _require_transaction(using)
    condition = Q(pk__in=[])
    for address in {address.lower() for address in addresses if address}:
        condition |= Q(recipient_address__iexact=address)
    by_type: defaultdict[str, set[object]] = defaultdict(set)
    for target in targets:
        label, pk = eligibility.object_reference(target)
        by_type[label].add(pk)
    for label, ids in by_type.items():
        condition |= Q(
            eligibility_object_type=label, eligibility_object_id__in=sorted(ids, key=str)
        )
    return (
        EmailOutbox.objects.using(using)
        .filter(condition, kind=Kind.SECURITY, status=Status.QUEUED)
        .update(status=Status.EXPIRED, body="", html_body="", wiped_at=Now())
    )
