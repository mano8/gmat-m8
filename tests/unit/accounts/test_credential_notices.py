"""A password or second factor that changes tells its owner (M8.3, X9 left the register).

allauth's notices (``ACCOUNT_EMAIL_NOTIFICATIONS``) and the administrator's MFA reset go through
the outbox like any account mail: sealed, wiped after sending, sent only to an active account
that still has the address. Nothing here opens an SMTP connection.
"""

from collections.abc import Callable

import pytest
from allauth.mfa.models import Authenticator
from django.conf import settings
from django.test import Client
from django.urls import reverse
from django.utils import translation

from apps.accounts import services
from apps.accounts.models import User
from apps.notifications.eligibility import ACCOUNT_ACTIVE
from apps.notifications.models import EmailOutbox, Kind
from tests.org import People
from tests.support import (
    PASSWORD,
    enroll_totp,
    mfa_login,
    password_login,
    principal_for,
    queued_security_mail,
)
from tests.unit.accounts.test_mfa import reauthenticated

pytestmark = pytest.mark.django_db

NEW_PASSWORD = "A-new-long-passphrase-7731"


def mail_to(address: str) -> list[tuple[str, str]]:
    return [(subject, body) for to, subject, body in queued_security_mail() if to == address]


@pytest.fixture
def english() -> object:
    with translation.override("en"):
        yield


def test_the_setting_is_on() -> None:
    assert settings.ACCOUNT_EMAIL_NOTIFICATIONS is True


class TestNotices:
    def test_a_password_change(self, client: Client, make_user: Callable[..., User]) -> None:
        user = make_user("tech@example.org")
        password_login(client, user.email)
        client.post(
            reverse("account_change_password"),
            {"oldpassword": PASSWORD, "password1": NEW_PASSWORD, "password2": NEW_PASSWORD},
        )
        [(subject, body)] = mail_to(user.email)
        assert subject
        assert "127.0.0.1" in body  # where it came from: what the owner needs to judge it
        assert NEW_PASSWORD not in body

    def test_it_is_queued_sealed_with_the_account_rule(
        self, client: Client, make_user: Callable[..., User]
    ) -> None:
        user = make_user("tech@example.org")
        password_login(client, user.email)
        client.post(
            reverse("account_change_password"),
            {"oldpassword": PASSWORD, "password1": NEW_PASSWORD, "password2": NEW_PASSWORD},
        )
        message = EmailOutbox.objects.get(recipient_address=user.email)
        assert message.kind == Kind.SECURITY
        assert message.sensitive is True
        assert message.eligibility_rule == ACCOUNT_ACTIVE
        assert "127.0.0.1" not in message.body  # sealed at rest

    @pytest.mark.usefixtures("frozen_totp")
    def test_a_second_factor_removed(self, client: Client, make_user: Callable[..., User]) -> None:
        user = make_user("tech@example.org")
        mfa_login(client, user.email, enroll_totp(user))
        assert mail_to(user.email) == []  # signing in is no change
        reauthenticated(client).post(reverse("mfa_deactivate_totp"))
        assert not Authenticator.objects.filter(user=user, type="totp").exists()
        assert len(mail_to(user.email)) == 1


class TestMfaReset:
    def test_the_owner_is_told_and_nothing_is_said_about_who(
        self, people: People, make_user: Callable[..., User], english: object
    ) -> None:
        user = make_user("lost-phone@example.org")
        enroll_totp(user)
        services.reset_user_mfa(principal_for(people.group_admin), user.pk, reason="Lost phone")
        [(subject, body)] = mail_to(user.email)
        assert "second factors" in subject
        assert "every session" in body
        assert people.group_admin.email not in body
        assert "Lost phone" not in body
        message = EmailOutbox.objects.get(recipient_address=user.email)
        assert message.source_ref == services.MFA_RESET_MAIL
        assert message.eligibility_rule == ACCOUNT_ACTIVE

    def test_a_refused_reset_says_nothing(
        self, people: People, make_user: Callable[..., User]
    ) -> None:
        user = make_user("lost-phone@example.org")
        with pytest.raises(Exception, match=r"reason|motivo|raison|Give"):
            services.reset_user_mfa(principal_for(people.group_admin), user.pk, reason=" ")
        assert mail_to(user.email) == []
