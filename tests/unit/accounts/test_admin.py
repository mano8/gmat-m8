"""The admin: allauth login, session MFA, read-only registrations, no deletes."""

from collections.abc import Callable
from typing import Any

import pytest
from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator
from django.contrib import admin
from django.contrib.auth.models import Group
from django.test import Client, RequestFactory
from django.urls import reverse

from apps.accounts.models import User
from apps.core.admin import ReadOnlyModelAdmin, ServiceBackedModelAdmin
from tests.support import enroll_totp, mfa_login, password_login

pytestmark = pytest.mark.django_db

ADMIN = "/admin/"


@pytest.fixture
def staff(make_user: Callable[..., User]) -> User:
    return make_user("ops@example.org", is_staff=True, is_superuser=True)


class TestAdminLogin:
    def test_anonymous_visitors_go_to_the_allauth_login(self, client: Client) -> None:
        response = client.get(ADMIN, follow=True)
        final_path, _ = response.redirect_chain[-1]
        assert final_path.startswith(reverse("account_login"))
        assert "next=" in final_path

    def test_the_django_admin_login_form_is_never_processed(
        self, client: Client, staff: User
    ) -> None:
        response = client.post(
            reverse("admin:login"), {"username": staff.email, "password": "a-long-test-password-1"}
        )
        assert response.status_code == 302
        assert response["Location"].startswith(reverse("account_login"))
        assert "_auth_user_id" not in client.session

    def test_non_staff_accounts_are_refused(
        self, client: Client, make_user: Callable[..., User]
    ) -> None:
        client.force_login(make_user())
        assert client.get(reverse("admin:login")).status_code == 403
        assert client.get(ADMIN).status_code == 302

    def test_password_only_session_is_sent_to_enroll(self, client: Client, staff: User) -> None:
        password_login(client, staff.email)
        response = client.get(ADMIN)
        assert response.status_code == 302
        assert response["Location"].startswith(reverse("mfa_index") + "?next=")

    def test_enrolled_but_unused_mfa_is_refused(self, client: Client, staff: User) -> None:
        enroll_totp(staff)
        client.force_login(staff)
        response = client.get(ADMIN)
        assert response.status_code == 302
        assert response["Location"].startswith(reverse("mfa_reauthenticate") + "?next=")
        assert client.post(ADMIN + "accounts/user/").status_code == 403

    def test_session_that_completed_mfa_is_admitted(
        self, client: Client, staff: User, frozen_totp: int
    ) -> None:
        secret = enroll_totp(staff)
        mfa_login(client, staff.email, secret)
        assert client.get(ADMIN).status_code == 200
        response = client.get(reverse("admin:login") + "?next=/admin/accounts/user/")
        assert response["Location"] == "/admin/accounts/user/"
        assert client.get(reverse("admin:login") + "?next=//evil.example/")["Location"] == ADMIN

    def test_logout_hands_over_to_allauth(
        self, client: Client, staff: User, frozen_totp: int
    ) -> None:
        mfa_login(client, staff.email, enroll_totp(staff))
        assert client.get(reverse("admin:logout"))["Location"] == reverse("account_logout")
        response = client.post(reverse("admin:logout"))
        assert response.status_code == 307
        assert response["Location"] == reverse("account_logout")


class TestRegistrations:
    def test_every_admin_is_service_backed(self) -> None:
        assert all(
            isinstance(model_admin, ServiceBackedModelAdmin)
            for model_admin in admin.site._registry.values()
        )

    def test_groups_are_not_registered(self) -> None:
        assert not admin.site.is_registered(Group)

    @pytest.mark.parametrize("model", [User, EmailAddress, Authenticator])
    def test_account_models_are_read_only(self, model: Any, staff: User) -> None:
        model_admin = admin.site._registry[model]
        assert isinstance(model_admin, ReadOnlyModelAdmin)
        request = RequestFactory().get(ADMIN)
        request.user = staff
        assert model_admin.has_add_permission(request) is False
        assert model_admin.has_change_permission(request) is False
        assert model_admin.has_delete_permission(request) is False
        assert "delete_selected" not in model_admin.get_actions(request)

    def test_mfa_secrets_are_never_shown(self) -> None:
        assert "data" in (admin.site._registry[Authenticator].exclude or ())

    def test_password_hashes_are_never_shown(self) -> None:
        assert "password" in (admin.site._registry[User].exclude or ())


class TestServiceBackedBase:
    def make(self) -> ServiceBackedModelAdmin:
        return ServiceBackedModelAdmin(Group, admin.site)

    def test_saving_requires_a_service(self) -> None:
        request = RequestFactory().post(ADMIN)
        with pytest.raises(NotImplementedError):
            self.make().save_model(request, Group(name="x"), None, change=False)

    def test_save_goes_through_the_service(self) -> None:
        calls: list[tuple[Any, bool]] = []

        class GroupAdmin(ServiceBackedModelAdmin):
            def save_via_service(self, request: Any, obj: Any, form: Any, change: bool) -> None:
                calls.append((obj, change))

        group = Group(name="x")
        GroupAdmin(Group, admin.site).save_model(RequestFactory().post(ADMIN), group, None, True)
        assert calls == [(group, True)]

    def test_deletes_are_impossible(self) -> None:
        request = RequestFactory().post(ADMIN)
        model_admin = self.make()
        assert model_admin.has_delete_permission(request) is False
        with pytest.raises(PermissionError):
            model_admin.delete_model(request, Group(name="x"))
        with pytest.raises(PermissionError):
            model_admin.delete_queryset(request, Group.objects.none())
