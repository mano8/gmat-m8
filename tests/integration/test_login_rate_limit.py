"""Failed logins in parallel, across separate cache clients, never exceed the limit."""

import threading
from collections.abc import Callable
from typing import Any

import pytest
from django.contrib.auth.signals import user_login_failed
from django.test import Client

from apps.accounts.models import User
from tests.integration.conftest import run_concurrently
from tests.support import password_login

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]

ATTEMPTS = 16
KEY_LIMIT = 5  # ACCOUNT_RATE_LIMITS["login_failed"]: 5 per 5 minutes per account


def test_parallel_failed_logins_never_exceed_the_limit(make_user: Callable[..., User]) -> None:
    make_user()
    checked: list[int] = []
    lock = threading.Lock()

    def count(sender: Any, credentials: Any, request: Any = None, **kwargs: Any) -> None:
        with lock:
            checked.append(1)

    user_login_failed.connect(count, dispatch_uid="test-count-password-checks")
    try:

        def attempt(index: int) -> int:
            # Two forwarded client addresses: the per-account limit applies to both.
            address = "203.0.113.9" if index % 2 else "198.51.100.7"
            response = password_login(
                Client(),
                "tech@example.org",
                f"wrong-password-{index}",
                HTTP_X_FORWARDED_FOR=address,
            )
            return int(response.status_code)

        statuses = run_concurrently(ATTEMPTS, attempt)
    finally:
        user_login_failed.disconnect(dispatch_uid="test-count-password-checks")

    assert all(status in {200, 429} for status in statuses), statuses
    assert 1 <= len(checked) <= KEY_LIMIT
