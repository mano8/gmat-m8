"""The invitation and email-change pages (plan §5 "Invitations", Web, Logging).

Token links store the token's hash in the session and redirect to a token-less page; the
pages carry ``no-store`` (and ``no-referrer`` while the URL holds the token); every unusable
token shows one generic page; the links are rate-limited.
"""

from collections.abc import Callable

import pytest
from django.core.cache import cache
from django.test import Client
from django.urls import reverse

from apps.access.principals import SessionPrincipal
from apps.access.roles import Role
from apps.accounts import services
from apps.accounts.models import EmailChangeRequest, Invitation, User
from apps.notifications.models import EmailOutbox
from apps.organizations.models import Membership
from tests.support import PASSWORD
from tests.tokens import last_token

pytestmark = pytest.mark.django_db

NEW_PASSWORD = "a-brand-new-password-9"
INVALID = "This link is not valid"


@pytest.fixture(autouse=True)
def _fresh_rate_limits() -> None:
    cache.clear()


def open_link(client: Client, path: str) -> str:
    """Follow a token link: return the token-less page it redirects to."""
    response = client.get(path)
    assert response.status_code == 302
    assert response["Referrer-Policy"] == "no-referrer"
    assert "no-store" in response["Cache-Control"]
    location = str(response["Location"])
    assert path.strip("/").rsplit("/", 1)[-1] not in location
    return location


def invitation_path(email: str) -> str:
    return reverse("invitations:link", args=[last_token(email)])


class TestInvitationPages:
    def test_new_account(self, invite: Callable[..., Invitation], branch: object) -> None:
        invite()
        client = Client(HTTP_ACCEPT_LANGUAGE="en")
        page = open_link(client, invitation_path("new@example.org"))
        response = client.get(page)
        assert response.status_code == 200 and b'name="password1"' in response.content
        assert "no-store" in response["Cache-Control"]
        # A form page keeps same-origin, so the POST's Origin passes the CSRF check.
        assert response.get("Referrer-Policy") != "no-referrer"
        mismatch = client.post(
            reverse("invitations:submit"),
            {"password1": NEW_PASSWORD, "password2": "other-pw-123456"},
        )
        assert mismatch.status_code == 200 and not User.objects.filter(email="new@example.org")
        weak = client.post(
            reverse("invitations:submit"), {"password1": "short", "password2": "short"}
        )
        assert weak.status_code == 200 and b"errorlist" in weak.content
        done = client.post(
            reverse("invitations:submit"), {"password1": NEW_PASSWORD, "password2": NEW_PASSWORD}
        )
        assert done.status_code == 302 and done["Location"] == reverse("account_login")
        assert User.objects.get(email="new@example.org").check_password(NEW_PASSWORD)
        # Single use: the page and a replayed submission show the generic error.
        assert INVALID.encode() in client.get(page).content

    def test_unknown_or_used_tokens_show_one_page(self, invite: Callable[..., Invitation]) -> None:
        client = Client(HTTP_ACCEPT_LANGUAGE="en")
        page = open_link(client, reverse("invitations:link", args=["A" * 43]))
        assert INVALID.encode() in client.get(page).content
        empty = Client(HTTP_ACCEPT_LANGUAGE="en")
        assert INVALID.encode() in empty.get(reverse("invitations:accept")).content
        assert INVALID.encode() in empty.post(reverse("invitations:submit")).content

    def test_existing_account_signed_in_as_itself(
        self, invite: Callable[..., Invitation], make_user: Callable[..., User]
    ) -> None:
        user = make_user("old@example.org")
        invite("old@example.org", role=Role.WAREHOUSE_OPERATOR)
        path = invitation_path("old@example.org")
        anonymous = Client(HTTP_ACCEPT_LANGUAGE="en")
        page = open_link(anonymous, path)
        assert b"Sign in with it" in anonymous.get(page).content
        other = Client(HTTP_ACCEPT_LANGUAGE="en")
        other.force_login(make_user("other@example.org"))
        open_link(other, path)
        assert b"another account" in other.get(page).content
        own = Client(HTTP_ACCEPT_LANGUAGE="en")
        own.force_login(user)
        open_link(own, path)
        response = own.get(page)
        assert b'name="password1"' not in response.content
        done = own.post(reverse("invitations:submit"), {"password1": "x", "password2": "x"})
        assert done.status_code == 302 and done["Location"] == reverse("core:home")
        user.refresh_from_db()
        assert user.check_password(PASSWORD)
        assert Membership.objects.get(user=user).role == Role.WAREHOUSE_OPERATOR

    def test_links_are_rate_limited(self) -> None:
        client = Client()
        path = reverse("invitations:link", args=["B" * 43])
        statuses = [client.get(path).status_code for _ in range(21)]
        assert statuses[:20] == [302] * 20 and statuses[20] == 429

    def test_the_language_switcher_never_returns_to_a_token_page(
        self, invite: Callable[..., Invitation]
    ) -> None:
        invite()
        client = Client()
        response = client.get(open_link(client, invitation_path("new@example.org")))
        assert response.context["language_next"] == "/"


class TestEmailChangePages:
    @pytest.fixture
    def change(
        self, admin_principal: SessionPrincipal, make_user: Callable[..., User]
    ) -> EmailChangeRequest:
        user = make_user("user@example.org")
        return services.request_email_change(admin_principal, user.pk, new_email="new@example.org")

    def test_confirm(self, change: EmailChangeRequest) -> None:
        client = Client()
        page = open_link(client, reverse("email_change:link", args=[last_token("new@example.org")]))
        assert page == reverse("email_change:confirm")
        response = client.get(page)
        assert response.status_code == 200 and b"new@example.org" in response.content
        assert "no-store" in response["Cache-Control"]
        assert client.post(reverse("email_change:confirm_submit")).status_code == 302
        change.refresh_from_db()
        assert change.confirmed_at is not None
        assert client.post(reverse("email_change:confirm_submit")).status_code == 200  # used

    def test_cancel(self, change: EmailChangeRequest) -> None:
        client = Client()
        link = reverse("email_change:link", args=[last_token("user@example.org")])
        page = open_link(client, link)
        assert page == reverse("email_change:cancel")
        assert client.get(page).status_code == 200
        # The cancel token never confirms; the error page also forgets the token.
        assert client.post(reverse("email_change:confirm_submit")).status_code == 200
        assert "_gmat_email_change" not in client.session
        change.refresh_from_db()
        assert change.confirmed_at is None
        open_link(client, link)
        assert client.post(reverse("email_change:cancel_submit")).status_code == 302
        change.refresh_from_db()
        assert change.cancelled_at is not None

    def test_unknown_token(self) -> None:
        client = Client(HTTP_ACCEPT_LANGUAGE="en")
        page = open_link(client, reverse("email_change:link", args=["C" * 43]))
        assert page == reverse("email_change:confirm")
        assert INVALID.encode() in client.get(page).content
        assert INVALID.encode() in client.get(reverse("email_change:cancel")).content


def test_inactive_users_receive_no_reset_email(make_user: Callable[..., User]) -> None:
    make_user("gone@example.org", is_active=False)
    response = Client().post(reverse("account_reset_password"), {"email": "gone@example.org"})
    assert response.status_code == 302
    assert not EmailOutbox.objects.exists()
