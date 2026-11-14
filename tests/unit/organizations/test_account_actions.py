"""A person's name and a lost second factor, from the person's page (M10.4, ADR-0030)."""

import re

import pytest
from allauth.mfa.models import Authenticator
from django.test import Client
from django.urls import reverse

from apps.access.roles import Role
from apps.accounts.models import User
from apps.accounts.services import MFA_RESET_OPERATION
from apps.audit.models import AuditEvent
from apps.core.idempotency import issue_key
from apps.notifications.models import EmailOutbox
from tests.org import People, client_for
from tests.support import enroll_totp, grant

pytestmark = pytest.mark.django_db

HTMX = {"HX-Request": "true"}


def page(client: Client, user: User) -> str:
    response = client.get(reverse("org:person", args=[user.pk]))
    assert response.status_code == 200
    return response.content.decode()


def reset_url(user: User) -> str:
    return reverse("org:reset_mfa", args=[user.pk])


def rename_url(user: User) -> str:
    return reverse("org:rename_person", args=[user.pk])


def form_key(html: str) -> str:
    found = re.search(r'id="id_mfa_reset_idempotency_key" value="([^"]+)"', html) or re.search(
        r'value="([^"]+)"[^>]*id="id_mfa_reset_idempotency_key"', html
    )
    assert found is not None, "the reset dialog carries its key"
    return found.group(1)


def factors(user: User) -> int:
    return Authenticator.objects.filter(user=user).count()


class TestThePage:
    def test_the_reset_is_offered_only_with_a_factor(self, people: People) -> None:
        client = client_for(people.group_admin)
        Authenticator.objects.filter(user=people.nobody).delete()
        assert reset_url(people.nobody) not in page(client, people.nobody)
        html = page(client, people.tech_mad)
        assert reset_url(people.tech_mad) in html
        assert 'data-testid="mfa-reset-open"' in html
        assert 'name="identity_checked"' in html

    def test_never_on_ones_own_page_nor_a_staff_account(self, people: People) -> None:
        client = client_for(people.group_admin)
        for user in (people.group_admin, people.operator):
            assert reset_url(user) not in page(client, user), user.email

    def test_not_even_a_superuser_is_offered_a_staff_reset(self, people: People) -> None:
        root = User.objects.create_user("root@example.org", is_staff=True, is_superuser=True)
        grant(root, Role.GROUP_ADMIN)
        enroll_totp(root)
        assert reset_url(people.operator) not in page(client_for(root), people.operator)

    def test_the_name_is_offered_to_a_person_and_on_ones_own_page(self, people: People) -> None:
        client = client_for(people.group_admin)
        for user in (people.tech_mad, people.group_admin):
            html = page(client, user)
            assert rename_url(user) in html and 'data-testid="rename-open"' in html
        assert rename_url(people.operator) not in page(client, people.operator)


class TestRename:
    def test_saves_the_name(self, people: People) -> None:
        client = client_for(people.group_admin)
        response = client.post(rename_url(people.tech_mad), {"full_name": "Tomás Ruiz"})
        assert response.status_code == 302
        assert response["Location"] == reverse("org:person", args=[people.tech_mad.pk])
        assert User.objects.get(pk=people.tech_mad.pk).full_name == "Tomás Ruiz"
        assert AuditEvent.objects.filter(action="user.renamed").count() == 1

    def test_a_blank_name_comes_back_in_the_dialog(self, people: People) -> None:
        client = client_for(people.group_admin)
        before = User.objects.get(pk=people.tech_mad.pk).full_name
        response = client.post(rename_url(people.tech_mad), {"full_name": "  "}, headers=HTMX)
        assert response.status_code == 422
        assert b'data-testid="rename-form"' in response.content
        assert User.objects.get(pk=people.tech_mad.pk).full_name == before

    def test_staff_and_other_roles_are_refused(self, people: People) -> None:
        admin = client_for(people.group_admin)
        assert admin.post(rename_url(people.operator), {"full_name": "Ops"}).status_code == 403
        manager = client_for(people.mm_mad)
        assert manager.post(rename_url(people.tech_mad), {"full_name": "X"}).status_code == 403
        assert admin.get(rename_url(people.tech_mad)).status_code == 405


class TestReset:
    def test_resets_from_the_dialog(self, people: People) -> None:
        tech = people.tech_mad
        client = client_for(people.group_admin)
        key = form_key(page(client, tech))
        data = {"reason": "Lost phone", "identity_checked": "on", "idempotency_key": key}
        response = client.post(reset_url(tech), data, headers=HTMX)
        assert response.status_code == 204
        assert response["HX-Redirect"] == reverse("org:person", args=[tech.pk])
        assert factors(tech) == 0
        event = AuditEvent.objects.get(action="user.mfa_reset")
        assert event.actor == people.group_admin
        assert event.data["identity_checked"] is True and event.data["reason"] == "Lost phone"
        assert EmailOutbox.objects.filter(recipient_address=tech.email).count() == 1
        # A double tap with the same form changes nothing more.
        assert client.post(reset_url(tech), data).status_code == 302
        assert AuditEvent.objects.filter(action="user.mfa_reset").count() == 1

    @pytest.mark.parametrize(
        "data",
        [{"reason": "Lost phone"}, {"identity_checked": "on"}],
        ids=["identity-unchecked", "no-reason"],
    )
    def test_an_incomplete_form_comes_back_and_leaves_the_factors(
        self, people: People, data: dict[str, str]
    ) -> None:
        tech = people.tech_mad
        client = client_for(people.group_admin)
        key = form_key(page(client, tech))
        response = client.post(reset_url(tech), {**data, "idempotency_key": key}, headers=HTMX)
        assert response.status_code == 422
        assert b'data-testid="mfa-reset-form"' in response.content
        assert factors(tech) == 1
        plain = client.post(reset_url(tech), {**data, "idempotency_key": key})
        assert plain.status_code == 302 and factors(tech) == 1

    def test_ones_own_factors_are_refused(self, people: People) -> None:
        me = people.group_admin
        client = client_for(me)
        assert reset_url(me) not in page(client, me)  # no dialog: a forged POST
        data = {
            "reason": "Lost",
            "identity_checked": "on",
            "idempotency_key": issue_key(me.pk, MFA_RESET_OPERATION),
        }
        response = client.post(reset_url(me), data, headers=HTMX)
        assert response.status_code == 422
        assert factors(me) == 1

    def test_a_staff_account_answers_403(self, people: People) -> None:
        data = {
            "reason": "Lost",
            "identity_checked": "on",
            "idempotency_key": issue_key(people.group_admin.pk, MFA_RESET_OPERATION),
        }
        response = client_for(people.group_admin).post(reset_url(people.operator), data)
        assert response.status_code == 403
        assert factors(people.operator) == 1

    def test_authority_revoked_between_the_page_and_the_post(self, people: People) -> None:
        tech = people.tech_mad
        client = client_for(people.group_admin)
        key = form_key(page(client, tech))
        people.group_admin.memberships.update(revoked_at=tech.date_joined)
        data = {"reason": "Lost", "identity_checked": "on", "idempotency_key": key}
        assert client.post(reset_url(tech), data).status_code == 403
        assert factors(tech) == 1
        assert not EmailOutbox.objects.filter(recipient_address=tech.email).exists()

    def test_needs_session_mfa_and_post(self, people: People) -> None:
        tech = people.tech_mad
        no_mfa = client_for(people.group_admin, mfa=False)
        response = no_mfa.post(reset_url(tech), {"reason": "Lost", "identity_checked": "on"})
        assert response.status_code == 403
        assert factors(tech) == 1
        assert client_for(people.group_admin).get(reset_url(tech)).status_code == 405
