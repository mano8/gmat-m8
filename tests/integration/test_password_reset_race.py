"""Two concurrent redemptions of one password-reset token: exactly one succeeds."""

import time
from collections.abc import Callable
from typing import Any

import pytest
from allauth.account.forms import default_token_generator
from allauth.account.utils import user_pk_to_url_str
from django.test import Client
from django.urls import reverse

from apps.accounts.adapters import AccountAdapter, PasswordResetTokenInvalid
from apps.accounts.models import User
from tests.integration.conftest import run_concurrently

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]

PASSWORDS = ["first-new-password-1", "second-new-password-2"]


@pytest.fixture
def slow_set_password(monkeypatch: pytest.MonkeyPatch) -> None:
    """Widen the race window: both redemptions verify before either could write."""
    original = AccountAdapter.set_password

    def slow(self: AccountAdapter, user: Any, password: str) -> None:
        time.sleep(0.3)
        original(self, user, password)

    monkeypatch.setattr(AccountAdapter, "set_password", slow)


def test_adapter_redemptions(make_user: Callable[..., User], slow_set_password: None) -> None:
    user = make_user()
    token = default_token_generator.make_token(user)

    def redeem(index: int) -> str:
        AccountAdapter().redeem_password_reset(user, token, PASSWORDS[index])
        return PASSWORDS[index]

    results = run_concurrently(2, redeem)
    winners = [result for result in results if isinstance(result, str)]
    losers = [result for result in results if isinstance(result, PasswordResetTokenInvalid)]
    assert len(winners) == 1 and len(losers) == 1
    user.refresh_from_db()
    assert user.check_password(winners[0])


def test_http_redemptions(make_user: Callable[..., User], slow_set_password: None) -> None:
    user = make_user()
    path = reverse(
        "account_reset_password_from_key",
        kwargs={
            "uidb36": user_pk_to_url_str(user),
            "key": default_token_generator.make_token(user),
        },
    )
    clients = [Client(), Client()]
    keyless = [client.get(path)["Location"] for client in clients]

    def submit(index: int) -> int:
        password = PASSWORDS[index]
        response = clients[index].post(
            keyless[index], {"password1": password, "password2": password}
        )
        return int(response.status_code)

    statuses = run_concurrently(2, submit)
    assert sorted(statuses) == [200, 302]
    winner = PASSWORDS[statuses.index(302)]
    user.refresh_from_db()
    assert user.check_password(winner)
