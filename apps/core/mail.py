"""Interim email backend for production until the outbox exists (M2.5).

CLAUDE.md forbids SMTP inside a request; all mail will go through ``EmailOutbox`` and the
worker. Until then production sends nothing, and never prints security mail (a reset
link) into container logs the way the console backend would. Production is not live
before M2.5, which replaces this backend and its system check.
"""

import logging
from collections.abc import Sequence
from typing import Any

from django.core.mail.backends.base import BaseEmailBackend

logger = logging.getLogger(__name__)


class DisabledEmailBackend(BaseEmailBackend):
    """Drops every message; logs only how many, never recipients or content."""

    def send_messages(self, email_messages: Sequence[Any]) -> int:
        if email_messages:
            logger.warning(
                "Email is disabled until the outbox exists (M2.5); %d message(s) dropped",
                len(email_messages),
            )
        return 0
