"""After a restore: new recovery codes before anything else (plan §9, F52), and the credential
changes a restore must not undo reach the audit trail (plan §9, step 5)."""

from collections.abc import Callable
from typing import Any

import pytest
from allauth.account import signals as account_signals
from allauth.mfa import signals as mfa_signals
from allauth.mfa.models import Authenticator
from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
from django.contrib.messages import get_messages
from django.test import Client, RequestFactory
from django.urls import reverse

from apps.accounts.mfa import settle_recovery_codes
from apps.accounts.models import User
from apps.audit.models import AuditEvent
from tests.org import People, client_for
from tests.support import PASSWORD, enroll_totp, mfa_login, password_login

pytestmark = pytest.mark.django_db


def owes(user: User) -> None:
    User.objects.filter(pk=user.pk).update(recovery_codes_owed=True)
    user.refresh_from_db()


class TestPrivileged:
    def test_a_page_leads_to_the_new_set_and_keeps_where_it_was_going(self, people: People) -> None:
        owes(people.mm_mad)
        response = client_for(people.mm_mad).get(reverse("core:home"))
        assert response.status_code == 302
        assert response["Location"] == (f"{reverse('mfa_generate_recovery_codes')}?next=%2F")

    def test_a_write_waits(self, people: People) -> None:
        owes(people.mm_mad)
        response = client_for(people.mm_mad).post(reverse("core:home"))
        assert response.status_code == 403

    def test_the_page_that_generates_them_stays_reachable(self, people: People) -> None:
        owes(people.mm_mad)
        response = client_for(people.mm_mad).get(reverse("mfa_generate_recovery_codes"))
        assert response.status_code != 403
        assert reverse("mfa_generate_recovery_codes") not in response.get("Location", "x?next=")

    def test_without_session_mfa_the_step_up_comes_first(self, people: People) -> None:
        owes(people.mm_mad)
        response = client_for(people.mm_mad, mfa=False).get(reverse("core:home"))
        assert reverse("mfa_reauthenticate") in response["Location"]

    def test_nothing_changes_when_nothing_is_owed(self, people: People) -> None:
        assert client_for(people.mm_mad).get(reverse("core:home")).status_code == 200

    def test_generating_them_settles_it(self, people: People) -> None:
        owes(people.mm_mad)
        RecoveryCodes.activate(people.mm_mad)
        mfa_signals.authenticator_reset.send(sender=Authenticator, request=None, user=people.mm_mad)
        people.mm_mad.refresh_from_db()
        assert not people.mm_mad.recovery_codes_owed
        assert client_for(people.mm_mad).get(reverse("core:home")).status_code == 200

    def test_a_factor_added_without_codes_settles_nothing(self, people: People) -> None:
        owes(people.mm_mad)
        settle_recovery_codes(people.mm_mad)
        people.mm_mad.refresh_from_db()
        assert people.mm_mad.recovery_codes_owed


class TestOthers:
    def test_an_account_without_a_privileged_role_is_only_told(
        self, people: People, frozen_totp: int, make_user: Callable[..., User]
    ) -> None:
        user = make_user("plain@example.org")
        secret = enroll_totp(user)
        owes(user)
        client = Client()
        response = mfa_login(client, user.email, secret)
        assert response.status_code == 302
        assert client.get(reverse("core:home")).status_code == 200

    def test_the_message_appears_after_the_login(self, make_user: Callable[..., User]) -> None:
        user = make_user("plain@example.org")
        owes(user)
        client = Client(headers={"accept-language": "en"})
        response = password_login(client, user.email, PASSWORD, follow=True)
        texts = [str(message) for message in get_messages(response.wsgi_request)]
        rendered = response.content.decode()
        assert any("recovery codes" in text for text in texts) or "recovery codes" in rendered


class TestCredentialAudit:
    def events(self, user: User, action: str) -> list[AuditEvent]:
        return list(AuditEvent.objects.filter(action=action, object_id=user.pk))

    @pytest.mark.parametrize(
        ("signal", "how"),
        [
            (account_signals.password_changed, "changed"),
            (account_signals.password_set, "set"),
            (account_signals.password_reset, "reset"),
        ],
    )
    def test_a_password_change_is_audited_without_the_password(
        self, make_user: Callable[..., User], signal: Any, how: str
    ) -> None:
        user = make_user("plain@example.org")
        request = RequestFactory().post("/", REMOTE_ADDR="10.0.0.7")
        signal.send(sender=User, request=request, user=user)
        [event] = self.events(user, "user.password_changed")
        assert event.data == {"how": how}
        assert (event.actor_id, event.via) == (user.pk, "allauth")
        assert event.object_type == "accounts.user"

    def test_a_real_password_reset_flow_is_audited_too(
        self, make_user: Callable[..., User]
    ) -> None:
        user = make_user("plain@example.org")
        client = client_for(user, mfa=False)
        response = client.post(
            reverse("account_change_password"),
            {
                "oldpassword": PASSWORD,
                "password1": "A-new-passphrase-2026!",
                "password2": "A-new-passphrase-2026!",
            },
        )
        assert response.status_code == 302
        [event] = self.events(user, "user.password_changed")
        assert event.data == {"how": "changed"}

    def test_a_removed_factor_is_audited_by_kind_and_ends_the_other_sessions(
        self, make_user: Callable[..., User]
    ) -> None:
        user = make_user("plain@example.org")
        enroll_totp(user)
        epoch = user.session_epoch
        authenticator = Authenticator.objects.get(user=user, type=Authenticator.Type.TOTP)
        mfa_signals.authenticator_removed.send(
            sender=Authenticator, request=None, user=user, authenticator=authenticator
        )
        [event] = self.events(user, "user.authenticator_removed")
        assert event.data == {"type": "totp"}
        assert event.ip is None
        user.refresh_from_db()
        assert user.session_epoch == epoch + 1
