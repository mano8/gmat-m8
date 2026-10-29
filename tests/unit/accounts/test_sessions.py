"""Session epochs: the database trigger, the guard middleware and session lifetimes."""

from collections.abc import Callable
from unittest import mock

import pytest
from allauth.account.internal.flows.login import AUTHENTICATION_METHODS_SESSION_KEY
from allauth.account.internal.stagekit import LOGIN_SESSION_KEY
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied
from django.test import Client, RequestFactory
from django.urls import reverse

from apps.accounts.adapters import AccountAdapter
from apps.accounts.models import User
from apps.accounts.sessions import (
    SESSION_EPOCH_KEY,
    SESSION_LOGIN_AT_KEY,
    bump_session_epoch,
    is_privileged,
    session_lifetime,
)
from tests.support import PASSWORD, password_login

pytestmark = pytest.mark.django_db


def counters(user: User) -> tuple[int, int]:
    user.refresh_from_db(fields=["session_epoch", "authz_generation"])
    return user.session_epoch, user.authz_generation


class TestSecurityCounterTrigger:
    def test_password_change_bumps_the_epoch(self, make_user: Callable[..., User]) -> None:
        user = make_user()
        user.set_password("another-long-password-2")
        user.save()
        assert counters(user) == (1, 0)

    def test_email_change_bumps_the_epoch(self, make_user: Callable[..., User]) -> None:
        user = make_user()
        User.objects.filter(pk=user.pk).update(email="new@example.org")
        assert counters(user) == (1, 0)

    def test_deactivation_bumps_both(self, make_user: Callable[..., User]) -> None:
        user = make_user()
        User.objects.filter(pk=user.pk).update(is_active=False)
        assert counters(user) == (1, 1)

    def test_reactivation_bumps_only_the_generation(self, make_user: Callable[..., User]) -> None:
        user = make_user(is_active=False)
        User.objects.filter(pk=user.pk).update(is_active=True)
        assert counters(user) == (0, 1)

    @pytest.mark.parametrize("flag", ["is_staff", "is_superuser"])
    def test_privilege_flags_bump_the_generation(
        self, make_user: Callable[..., User], flag: str
    ) -> None:
        user = make_user()
        User.objects.filter(pk=user.pk).update(**{flag: True})
        assert counters(user) == (0, 1)

    def test_a_stale_copy_cannot_move_the_counters_back(
        self, make_user: Callable[..., User]
    ) -> None:
        user = make_user()
        stale = User.objects.get(pk=user.pk)
        bump_session_epoch(user)
        User.objects.filter(pk=user.pk).update(is_staff=True)
        stale.full_name = "Renamed"
        stale.save()  # writes session_epoch=0, authz_generation=0 and is_staff=False
        # The epoch stays; reverting is_staff is itself an authority change: bumped again.
        assert counters(user) == (1, 2)

    def test_unrelated_updates_bump_nothing(self, make_user: Callable[..., User]) -> None:
        user = make_user()
        user.full_name = "Tech One"
        user.save()
        client = Client()
        password_login(client, user.email)  # updates last_login
        assert counters(user) == (0, 0)


class TestSessionGuard:
    def test_login_binds_the_session_to_the_current_epoch(
        self, client: Client, make_user: Callable[..., User]
    ) -> None:
        user = make_user()
        bump_session_epoch(user)
        stale = User.objects.get(pk=user.pk)
        User.objects.filter(pk=user.pk).update(session_epoch=5)
        client.force_login(stale)  # the in-memory copy says 1; the database says 5
        assert client.session[SESSION_EPOCH_KEY] == 5
        assert client.get(reverse("core:home")).status_code == 200

    def test_a_bumped_epoch_ends_the_session(
        self, client: Client, make_user: Callable[..., User]
    ) -> None:
        user = make_user()
        client.force_login(user)
        assert client.get(reverse("core:home")).status_code == 200
        bump_session_epoch(user)
        response = client.get(reverse("core:home"))
        assert response.status_code == 302
        assert response["Location"].startswith(reverse("account_login"))
        assert "_auth_user_id" not in client.session

    def test_a_session_without_an_epoch_is_ended(
        self, client: Client, make_user: Callable[..., User]
    ) -> None:
        client.force_login(make_user())
        session = client.session
        del session[SESSION_EPOCH_KEY]
        session.save()
        assert client.get(reverse("core:home")).status_code == 302

    def test_deactivation_ends_the_session(
        self, client: Client, make_user: Callable[..., User]
    ) -> None:
        user = make_user()
        client.force_login(user)
        User.objects.filter(pk=user.pk).update(is_active=False)
        assert client.get(reverse("core:home")).status_code == 302

    @pytest.mark.parametrize(
        ("extra", "lifetime"),
        [({}, 14 * 24 * 3600), ({"is_staff": True}, 12 * 3600)],
    )
    def test_sessions_end_after_their_lifetime(
        self, client: Client, make_user: Callable[..., User], extra: dict[str, bool], lifetime: int
    ) -> None:
        user = make_user(**extra)
        assert session_lifetime(user) == lifetime
        client.force_login(user)
        login_at = client.session[SESSION_LOGIN_AT_KEY]
        with mock.patch("apps.accounts.sessions.time.time", return_value=login_at + lifetime - 5):
            assert client.get(reverse("core:home")).status_code == 200
        with mock.patch("apps.accounts.sessions.time.time", return_value=login_at + lifetime + 1):
            assert client.get(reverse("core:home")).status_code == 302

    def test_privileged_means_staff_or_superuser_for_now(
        self, make_user: Callable[..., User]
    ) -> None:
        assert not is_privileged(make_user())
        assert is_privileged(make_user("ops@example.org", is_staff=True))
        assert is_privileged(make_user("root@example.org", is_superuser=True))

    def test_a_login_without_a_request_is_ignored(self, make_user: Callable[..., User]) -> None:
        from apps.accounts.signals import on_user_logged_in

        on_user_logged_in(sender=User, request=None, user=make_user())

    def test_login_sets_the_cookie_expiry_to_the_lifetime(
        self, client: Client, make_user: Callable[..., User]
    ) -> None:
        user = make_user("ops@example.org", is_staff=True)
        client.force_login(user)
        assert client.session.get_expiry_age() == 12 * 3600


class TestFreshLogin:
    def test_a_new_anonymous_login_discards_pending_state(
        self, client: Client, make_user: Callable[..., User]
    ) -> None:
        user = make_user()
        request = RequestFactory().post(reverse("account_login"))
        request.user = AnonymousUser()
        request.session = client.session
        request.session[LOGIN_SESSION_KEY] = {"stale": True}
        request.session[AUTHENTICATION_METHODS_SESSION_KEY] = [{"method": "password"}]
        authenticated = AccountAdapter(request).authenticate(
            request, email=user.email, password=PASSWORD
        )
        assert authenticated == user
        assert LOGIN_SESSION_KEY not in request.session
        assert AUTHENTICATION_METHODS_SESSION_KEY not in request.session

    def test_reauthentication_keeps_the_session_state(
        self, client: Client, make_user: Callable[..., User]
    ) -> None:
        user = make_user()
        request = RequestFactory().post("/")
        request.user = user
        request.session = client.session
        request.session[AUTHENTICATION_METHODS_SESSION_KEY] = [{"method": "password"}]
        AccountAdapter(request).authenticate(request, email=user.email, password=PASSWORD)
        assert request.session[AUTHENTICATION_METHODS_SESSION_KEY] == [{"method": "password"}]

    def test_an_unknown_client_ip_is_refused(self) -> None:
        request = RequestFactory().get("/", REMOTE_ADDR="")
        with pytest.raises(PermissionDenied):
            AccountAdapter(request).get_client_ip(request)
