"""Session epochs: the database trigger, the guard middleware and session lifetimes."""

from collections.abc import Callable
from typing import Any
from unittest import mock

import pytest
from allauth.account.internal.flows.login import AUTHENTICATION_METHODS_SESSION_KEY
from allauth.account.internal.stagekit import LOGIN_SESSION_KEY
from allauth.usersessions.models import UserSession
from django.contrib.auth.models import AnonymousUser
from django.contrib.sessions.backends.db import SessionStore
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.test import Client, RequestFactory
from django.urls import reverse

from apps.access.roles import Role
from apps.accounts.adapters import AccountAdapter
from apps.accounts.middleware import SessionGuardMiddleware
from apps.accounts.models import User
from apps.accounts.sessions import (
    SESSION_EPOCH_KEY,
    SESSION_LOGIN_AT_KEY,
    SESSION_SEEN_AT_KEY,
    bump_session_epoch,
    session_lifetime,
    session_limits,
    session_passive,
    touch_session,
)
from apps.organizations import views as org_views
from apps.organizations.models import Branch
from tests.support import PASSWORD, grant, password_login

pytestmark = pytest.mark.django_db


TOUCH_INTERVAL = 60


@pytest.fixture
def activity_is_written(settings: Any) -> None:
    """The test settings never write it: a slow run must not change a page's queries."""
    settings.SESSION_TOUCH_INTERVAL = TOUCH_INTERVAL


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
        session = client.session  # active all along: only the age is at stake here
        session[SESSION_SEEN_AT_KEY] = login_at + lifetime - 10
        session.save()
        # A privileged session without MFA is sent to the step-up, but stays logged in.
        with mock.patch("apps.accounts.sessions.time.time", return_value=login_at + lifetime - 5):
            client.get(reverse("core:home"))
            assert "_auth_user_id" in client.session
        with mock.patch("apps.accounts.sessions.time.time", return_value=login_at + lifetime + 1):
            assert client.get(reverse("core:home")).status_code == 302
            assert "_auth_user_id" not in client.session

    @pytest.mark.usefixtures("activity_is_written")
    @pytest.mark.parametrize(
        ("extra", "idle"),
        [({}, 2 * 3600), ({"is_staff": True}, 30 * 60)],
    )
    def test_an_idle_session_ends_long_before_its_age(
        self,
        client: Client,
        make_user: Callable[..., User],
        extra: dict[str, bool],
        idle: int,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """M8.3, X6: 2 hours, 30 minutes for a privileged account."""
        user = make_user(**extra)
        assert session_limits(user)[1] == idle
        client.force_login(user)
        login_at = client.session[SESSION_LOGIN_AT_KEY]
        with mock.patch("apps.accounts.sessions.time.time", return_value=login_at + idle - 5):
            client.get(reverse("core:home"))
            assert "_auth_user_id" in client.session
        # That request counted, so the session lives on from it.
        with mock.patch("apps.accounts.sessions.time.time", return_value=login_at + 2 * idle - 10):
            client.get(reverse("core:home"))
            assert "_auth_user_id" in client.session
        with (
            caplog.at_level("INFO", logger="gmat.security"),
            mock.patch("apps.accounts.sessions.time.time", return_value=login_at + 3 * idle),
        ):
            assert client.get(reverse("core:home")).status_code == 302
            assert "_auth_user_id" not in client.session
        events = [getattr(record, "security", {}) for record in caplog.records]
        assert [item["reason"] for item in events if item.get("event") == "session_ended"] == [
            "idle"
        ]

    @pytest.mark.usefixtures("activity_is_written")
    def test_the_activity_is_written_at_most_once_a_minute(
        self, client: Client, make_user: Callable[..., User]
    ) -> None:
        user = make_user()
        client.force_login(user)
        start = client.session[SESSION_SEEN_AT_KEY]
        with mock.patch(
            "apps.accounts.sessions.time.time", return_value=start + TOUCH_INTERVAL - 1
        ):
            client.get(reverse("core:home"))
        assert client.session[SESSION_SEEN_AT_KEY] == start
        with mock.patch("apps.accounts.sessions.time.time", return_value=start + TOUCH_INTERVAL):
            client.get(reverse("core:home"))
        assert client.session[SESSION_SEEN_AT_KEY] == start + TOUCH_INTERVAL

    @pytest.mark.usefixtures("activity_is_written")
    def test_what_a_page_sends_by_itself_is_not_activity(
        self, make_user: Callable[..., User]
    ) -> None:
        """A page left open with a polling badge must still go idle."""
        guard = SessionGuardMiddleware(lambda request: HttpResponse())
        user = make_user()
        request = RequestFactory().get("/")
        request.user = user
        session = SessionStore()
        session[SESSION_LOGIN_AT_KEY] = 0
        session[SESSION_SEEN_AT_KEY] = 0
        request.session = session
        guard.process_view(request, session_passive(lambda request: None), (), {})
        assert session[SESSION_SEEN_AT_KEY] == 0
        guard.process_view(request, lambda request: None, (), {})
        assert session[SESSION_SEEN_AT_KEY] > 0

    def test_the_delivery_polls_are_marked_passive(self) -> None:
        for name in ("invitation_delivery", "email_change_delivery"):
            assert getattr(getattr(org_views, name), "session_passive", False), name

    @pytest.mark.usefixtures("activity_is_written")
    def test_activity_without_a_client_address_only_notes_the_time(
        self, make_user: Callable[..., User]
    ) -> None:
        request = RequestFactory().get("/", REMOTE_ADDR="")
        request.user = make_user()
        session = SessionStore()
        session.create()
        request.session = session
        touch_session(request)
        assert session[SESSION_SEEN_AT_KEY] > 0
        assert not UserSession.objects.exists()

    def test_privileged_roles_have_short_sessions(
        self, make_user: Callable[..., User], branch: Branch
    ) -> None:
        manager = make_user("mm@example.org")
        technician = make_user()
        grant(manager, Role.MATERIAL_MANAGER, branch=branch)
        grant(technician, Role.TECHNICIAN, branch=branch)
        assert session_lifetime(manager) == 12 * 3600
        assert session_lifetime(technician) == 14 * 24 * 3600

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
