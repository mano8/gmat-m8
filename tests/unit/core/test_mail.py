import logging

import pytest
from django.core.mail import EmailMessage

from apps.core.mail import DisabledEmailBackend

SECRET_LINK = "https://app.example.org/accounts/password/reset/key/1-abc123secret/"


def test_drops_messages_and_logs_only_the_count(caplog: pytest.LogCaptureFixture) -> None:
    message = EmailMessage("Reset", SECRET_LINK, to=["tech@example.org"])
    with caplog.at_level(logging.WARNING, logger="apps.core.mail"):
        assert DisabledEmailBackend().send_messages([message]) == 0
    assert "1 message(s) dropped" in caplog.text
    assert SECRET_LINK not in caplog.text
    assert "tech@example.org" not in caplog.text


def test_nothing_to_send_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="apps.core.mail"):
        assert DisabledEmailBackend().send_messages([]) == 0
    assert caplog.text == ""
