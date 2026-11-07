"""One's own sessions: the list, ending them, and what cleans up after them (M8.3, X5)."""

import datetime
from collections.abc import Callable

import pytest
from allauth.usersessions.models import UserSession
from django.conf import settings
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.core.exceptions import ValidationError
from django.db.models import F
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.access.exceptions import MFARequired, StaleSession
from apps.access.principals import OperatorPrincipal, SessionPrincipal
from apps.access.roles import OWN_ACCOUNT, Role
from apps.accounts import housekeeping, selectors, services
from apps.accounts.models import User
from apps.accounts.sessions import SESSION_SEEN_AT_KEY
from apps.audit.models import AuditEvent
from apps.organizations.models import Branch
from apps.recovery import services as recovery_services
from tests.org import People, client_for, session_for
from tests.support import PASSWORD, grant, password_login, principal_for

pytestmark = pytest.mark.django_db


def row_for(user: User, session_key: str, *, ip: str = "203.0.113.9") -> UserSession:
    """The row a login writes for its session."""
    row: UserSession = UserSession.objects.create(
        user=user, session_key=session_key, ip=ip, user_agent="Firefox on a phone"
    )
    return row


def key_of(client: Client) -> str:
    return client.cookies[settings.SESSION_COOKIE_NAME].value


def signed_in(user: User) -> tuple[Client, UserSession]:
    client = client_for(user)
    return client, row_for(user, key_of(client))


class TestTheLoginWritesTheRow:
    def test_a_login_lists_the_session_and_a_logout_removes_it(
        self, client: Client, make_user: Callable[..., User]
    ) -> None:
        user = make_user()
        password_login(client, user.email, PASSWORD)
        row = UserSession.objects.get(user=user)
        assert row.session_key == client.session.session_key
        assert row.ip == "127.0.0.1"
        client.post(reverse("account_logout"))
        assert not UserSession.objects.filter(user=user).exists()


class TestList:
    def test_only_sessions_that_still_work_are_listed_the_current_first(
        self, people: People
    ) -> None:
        user = people.tech_mad
        _client, current = signed_in(user)
        other = row_for(user, session_for(user), ip="198.51.100.7")
        row_for(user, "no-such-session")  # its session ended without a logout
        stale_key = session_for(user)
        row_for(user, stale_key)
        stale = SessionStore(stale_key)
        stale[SESSION_SEEN_AT_KEY] = 0  # idle for decades
        stale.save()
        listed = selectors.own_sessions(user, current.session_key)
        assert [(item.pk, item.current) for item in listed] == [
            (current.pk, True),
            (other.pk, False),
        ]

    def test_sessions_of_an_old_epoch_are_gone_from_the_list(self, people: People) -> None:
        user = people.tech_mad
        key = session_for(user)
        row_for(user, key)
        assert selectors.own_sessions(user, None)
        User.objects.filter(pk=user.pk).update(session_epoch=F("session_epoch") + 1)
        user.refresh_from_db()
        assert selectors.own_sessions(user, None) == []

    def test_the_page_shows_ones_own_and_nobody_elses(self, people: People) -> None:
        client, _current = signed_in(people.tech_mad)
        row_for(people.tech_mad, session_for(people.tech_mad), ip="198.51.100.7")
        row_for(people.mm_mad, session_for(people.mm_mad), ip="192.0.2.44")
        html = client.get(reverse("sessions:list")).content.decode()
        assert html.count('data-testid="session-row"') == 2
        assert html.count('data-testid="session-current"') == 1
        assert html.count('data-testid="session-end"') == 1
        assert "198.51.100.7" in html
        assert "192.0.2.44" not in html
        assert 'data-testid="sessions-end-others"' in html

    def test_alone_there_is_nothing_to_end(self, people: People) -> None:
        client, _current = signed_in(people.tech_mad)
        html = client.get(reverse("sessions:list")).content.decode()
        assert 'data-testid="session-end"' not in html
        assert 'data-testid="sessions-end-others"' not in html

    def test_an_account_without_a_membership_has_the_page_too(
        self, make_user: Callable[..., User]
    ) -> None:
        client = client_for(make_user("nobody@example.org"), mfa=False)
        assert client.get(reverse("sessions:list")).status_code == 200


class TestEnding:
    def test_one_other_session(self, people: People) -> None:
        user = people.tech_mad
        client, current = signed_in(user)
        other_key = session_for(user)
        other = row_for(user, other_key)
        response = client.post(reverse("sessions:end", kwargs={"session_id": other.pk}))
        assert response.status_code == 302
        assert not Session.objects.filter(session_key=other_key).exists()
        assert list(UserSession.objects.filter(user=user)) == [current]
        event = AuditEvent.objects.get(action="user.sessions_ended")
        assert (event.object_id, event.data["count"]) == (user.pk, 1)

    def test_the_ended_session_is_out_on_its_next_request(self, people: People) -> None:
        user = people.tech_mad
        client, _current = signed_in(user)
        elsewhere = client_for(user)
        other = row_for(user, key_of(elsewhere))
        assert elsewhere.get(reverse("core:home")).status_code == 200
        client.post(reverse("sessions:end", kwargs={"session_id": other.pk}))
        assert elsewhere.get(reverse("core:home")).status_code == 302

    def test_every_other_session(self, people: People) -> None:
        user = people.tech_mad
        client, current = signed_in(user)
        keys = [session_for(user), session_for(user)]
        for key in keys:
            row_for(user, key)
        client.post(reverse("sessions:end_others"))
        assert not Session.objects.filter(session_key__in=keys).exists()
        assert client.get(reverse("core:home")).status_code == 200
        assert list(UserSession.objects.filter(user=user)) == [current]
        assert AuditEvent.objects.get(action="user.sessions_ended").data["count"] == 2

    def test_nothing_to_end_writes_nothing(self, people: People) -> None:
        client, _current = signed_in(people.tech_mad)
        client.post(reverse("sessions:end_others"))
        assert not AuditEvent.objects.filter(action="user.sessions_ended").exists()

    def test_someone_elses_session_is_not_touched_and_looks_already_ended(
        self, people: People
    ) -> None:
        client, _current = signed_in(people.tech_mad)
        key = session_for(people.mm_mad)
        theirs = row_for(people.mm_mad, key)
        response = client.post(reverse("sessions:end", kwargs={"session_id": theirs.pk}))
        unknown = client.post(reverse("sessions:end", kwargs={"session_id": 999_999}))
        assert (response.status_code, unknown.status_code) == (302, 302)
        assert response["Location"] == unknown["Location"]
        assert Session.objects.filter(session_key=key).exists()
        assert UserSession.objects.filter(pk=theirs.pk).exists()
        assert not AuditEvent.objects.filter(action="user.sessions_ended").exists()

    def test_the_current_session_ends_by_signing_out(self, people: People) -> None:
        client, current = signed_in(people.tech_mad)
        with pytest.raises(ValidationError) as refused:
            services.end_own_session(
                principal_for(people.tech_mad), current.pk, current_key=current.session_key
            )
        assert refused.value.code == "current"
        response = client.post(reverse("sessions:end", kwargs={"session_id": current.pk}))
        assert response.status_code == 302
        assert UserSession.objects.filter(pk=current.pk).exists()
        assert client.get(reverse("core:home")).status_code == 200

    def test_ends_are_post_only(self, people: People) -> None:
        client, current = signed_in(people.tech_mad)
        assert client.get(reverse("sessions:end_others")).status_code == 405
        assert (
            client.get(reverse("sessions:end", kwargs={"session_id": current.pk})).status_code
            == 405
        )

    def test_the_security_log_has_it(
        self, people: People, caplog: pytest.LogCaptureFixture
    ) -> None:
        client, _current = signed_in(people.tech_mad)
        row_for(people.tech_mad, session_for(people.tech_mad))
        with caplog.at_level("INFO", logger="gmat.security"):
            client.post(reverse("sessions:end_others"))
        (record,) = [
            item
            for item in caplog.records
            if getattr(item, "security", {}).get("event") == "sessions_ended"
        ]
        assert record.security["count"] == "1"  # type: ignore[attr-defined]


class TestAuthorization:
    def test_a_session_older_than_a_credential_change_is_refused(self, people: People) -> None:
        principal = principal_for(people.tech_mad)
        User.objects.filter(pk=people.tech_mad.pk).update(session_epoch=F("session_epoch") + 1)
        with pytest.raises(StaleSession):
            services.end_other_sessions(principal, current_key="x")

    def test_a_privileged_account_needs_session_mfa(
        self, make_user: Callable[..., User], branch: Branch
    ) -> None:
        user = make_user("mm@example.org")
        grant(user, Role.MATERIAL_MANAGER, branch=branch)
        without = SessionPrincipal(
            user_id=user.pk, email=user.email, session_epoch=user.session_epoch, mfa_at=None
        )
        with pytest.raises(MFARequired):
            services.end_other_sessions(without, current_key="x")
        assert services.end_other_sessions(principal_for(user), current_key="x") == 0

    def test_it_is_a_sessions_right_not_an_operators(self, people: People) -> None:
        from apps.access.authorization import authorize_in_txn
        from apps.access.exceptions import AccessDenied
        from apps.core.db import atomic_with_lock_timeout
        from apps.core.locking import LockOrder

        operator = OperatorPrincipal(
            user_id=people.group_admin.pk,
            email=people.group_admin.email,
            command="test",
            capability="org.manage",
        )
        with atomic_with_lock_timeout(), pytest.raises(AccessDenied):
            authorize_in_txn(operator, OWN_ACCOUNT, locks=LockOrder())

    def test_a_group_admin_ends_only_their_own(self, people: People) -> None:
        key = session_for(people.tech_mad)
        row_for(people.tech_mad, key)
        client, _current = signed_in(people.group_admin)
        client.post(reverse("sessions:end_others"))
        assert Session.objects.filter(session_key=key).exists()


class TestCleanup:
    def test_expired_sessions_and_rows_without_a_session_go(self, people: People) -> None:
        user = people.tech_mad
        kept = session_for(user)
        row_for(user, kept)
        expired = session_for(user)
        row_for(user, expired)
        Session.objects.filter(session_key=expired).update(
            expire_date=timezone.now() - datetime.timedelta(minutes=1)
        )
        row_for(user, "no-such-session")
        assert housekeeping.purge_ended_sessions() == 3  # one session, two rows
        assert not Session.objects.filter(session_key=expired).exists()
        assert list(UserSession.objects.values_list("session_key", flat=True)) == [kept]

    def test_it_is_a_registered_task(self) -> None:
        from apps.notifications.housekeeping import tasks

        assert tasks()[housekeeping.SESSIONS_TASK_NAME] is housekeeping.purge_ended_sessions

    def test_a_restore_ends_every_row_too(self, people: People) -> None:
        row_for(people.tech_mad, session_for(people.tech_mad))
        report = recovery_services.Report(failed_state_used=False)
        recovery_services._end_sessions(report)
        assert not UserSession.objects.exists()
        assert not Session.objects.exists()
