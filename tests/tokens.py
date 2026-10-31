"""Helpers for the invitation and email-change tests: the raw tokens live only in the email."""

import re

from apps.accounts.tokens import TOKEN_PATTERN
from tests.support import queued_security_mail

LINK = re.compile(rf"https?://[^\s]+/(?:invite|account/email-change)/({TOKEN_PATTERN})/")


def tokens_sent_to(address: str) -> list[str]:
    """Every token mailed to ``address``, oldest first."""
    found = []
    for recipient, _subject, body in queued_security_mail():
        if recipient.lower() == address.lower():
            found.extend(LINK.findall(body))
    return found


def last_token(address: str) -> str:
    tokens = tokens_sent_to(address)
    assert tokens, f"no token was mailed to {address}"
    return tokens[-1]
