"""The default mailer: whatever Django or allauth sends becomes an outbox row (plan §5).

``MAILERS["default"]`` is this backend, so no request ever opens an SMTP connection: it
writes rows, in the caller's transaction when there is one, and the worker sends them later
through ``MAILERS["outbound"]``.

Mail that arrives through Django's mail API is account mail (allauth: password reset and
account notices), so each message is queued as a sensitive ``SECURITY`` email to the one
account it is addressed to, with the ``account.active`` rule: it is sent only while that
account is active and still has this address. Business email never comes this way; services
call ``apps.notifications.outbox`` with user ids and an eligibility rule.

What the outbox cannot reproduce faithfully is refused (``UnsupportedMessage``) rather than
silently changed: several recipients, attachments, extra headers or a reply-to address.
"""

import logging
from collections.abc import Sequence
from email.utils import parseaddr
from typing import Any

from django.core.mail.backends.base import BaseEmailBackend
from django.db import transaction

from apps.accounts.models import User
from apps.notifications.eligibility import ACCOUNT_ACTIVE
from apps.notifications.outbox import queue_security_email

logger = logging.getLogger(__name__)

SOURCE_REF = "django-mail"


class UnsupportedMessage(ValueError):
    """The message uses a feature the outbox does not store."""


def _recipient(message: Any) -> str:
    recipients = list(message.recipients())
    if len(recipients) != 1:
        raise UnsupportedMessage("An account email has exactly one recipient")
    _, address = parseaddr(str(recipients[0]))
    if "@" not in address:
        raise UnsupportedMessage("The recipient is not an email address")
    return address


def _bodies(message: Any) -> tuple[str, str]:
    """The plain-text and HTML bodies of ``message``."""
    if message.attachments or message.extra_headers or message.reply_to:
        raise UnsupportedMessage("Attachments, extra headers and reply-to are not supported")
    text, html = str(message.body), ""
    if getattr(message, "content_subtype", "plain") == "html":
        text, html = "", text
    for content, mimetype in getattr(message, "alternatives", ()):
        if mimetype != "text/html" or html:
            raise UnsupportedMessage(f"Unsupported alternative {mimetype!r}")
        html = str(content)
    return text, html


class OutboxEmailBackend(BaseEmailBackend):
    """Queues messages; never sends. Returns how many messages were queued."""

    def send_messages(self, email_messages: Sequence[Any]) -> int:
        if not email_messages:
            return 0
        queued = 0
        with transaction.atomic():
            for message in email_messages:
                address = _recipient(message)
                text, html = _bodies(message)
                user = User.objects.filter(email=address.lower()).first()
                if user is None:
                    # Never happens through allauth (unknown accounts get no mail); nothing
                    # to check the address against, so nothing is queued.
                    logger.warning("An email to an address without an account was dropped")
                    continue
                queue_security_email(
                    address=address,
                    target=user,
                    rule=ACCOUNT_ACTIVE,
                    subject=str(message.subject),
                    body=text,
                    html_body=html,
                    source_ref=SOURCE_REF,
                    from_email=message.from_email,
                )
                queued += 1
        return queued
