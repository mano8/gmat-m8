"""Group-administrator screens (M2.6): people, memberships, offboarding, email changes,
invitations."""

import re

import pytest
from django.urls import reverse

from apps.access.roles import Role
from apps.accounts.models import EmailChangeRequest, Invitation, User
from apps.core.idempotency import issue_key
from apps.organizations.models import Membership
from tests.org import Org, People, client_for

pytestmark = pytest.mark.django_db


def emails(response: object) -> set[str]:
    return set(re.findall(r">([^<>\s]+@example\.org)</a>", response.content.decode()))  # type: ignore[attr-defined]


def messages_of(response: object) -> list[str]:
    return [str(message) for message in response.context["messages"]]  # type: ignore[attr-defined]


class TestPeople:
    def test_list_filters_and_partial(self, people: People) -> None:
        client = client_for(people.group_admin)
        everyone = client.get(reverse("org:people"))
        assert everyone.status_code == 200 and people.partner.email in emails(everyone)
        assert emails(client.get(reverse("org:people"), {"q": "mm-"})) == {
            "mm-mad@example.org",
            "mm-sev@example.org",
        }
        assert emails(client.get(reverse("org:people"), {"status": "inactive"})) == {
            "gone@example.org"
        }
        assert "gone@example.org" not in emails(
            client.get(reverse("org:people"), {"status": "active"})
        )
        partial = client.get(reverse("org:people"), {"q": "partner"}, HTTP_HX_REQUEST="true")
        assert partial.content.lstrip().startswith(b'<div id="people-results">')

    def test_only_group_administrators(self, people: People) -> None:
        for name in ("mm_mad", "operator", "tech_mad"):
            user = getattr(people, name)
            client = client_for(user, mfa=True)
            assert client.get(reverse("org:people")).status_code == 403, name
            assert client.get(reverse("org:person", args=[user.pk])).status_code == 403, name

    def test_staff_accounts_are_shown_without_actions(self, people: People) -> None:
        response = client_for(people.group_admin).get(
            reverse("org:person", args=[people.operator.pk])
        )
        assert response.status_code == 200
        assert reverse("org:offboard", args=[people.operator.pk]).encode() not in response.content
        own = client_for(people.group_admin).get(
            reverse("org:person", args=[people.group_admin.pk])
        )
        assert reverse("org:offboard", args=[people.group_admin.pk]).encode() not in own.content


class TestMemberships:
    def test_grant_validity_and_revoke(self, org: Org, people: People) -> None:
        client = client_for(people.group_admin)
        target = people.nobody
        key = issue_key(people.group_admin.pk, "membership.grant")
        granted = client.post(
            reverse("org:grant_membership", args=[target.pk]),
            {"role": Role.WAREHOUSE_OPERATOR, "branch": org.a_sev.pk, "idempotency_key": key},
        )
        assert granted.status_code == 302
        membership = Membership.objects.get(user=target)
        assert (membership.role, membership.branch) == (Role.WAREHOUSE_OPERATOR, org.a_sev)
        validity = client.post(
            reverse("org:membership_validity", args=[membership.pk]),
            {"valid_from": "2026-01-01T00:00", "valid_until": "2031-01-01T00:00"},
        )
        assert validity.status_code == 302
        membership.refresh_from_db()
        assert membership.valid_until is not None and membership.valid_until.year == 2031
        revoked = client.post(reverse("org:revoke_membership", args=[membership.pk]))
        assert revoked.status_code == 302
        membership.refresh_from_db()
        assert membership.revoked_at is not None and membership.revoked_by == people.group_admin
        again = client.post(reverse("org:revoke_membership", args=[membership.pk]), follow=True)
        assert again.status_code == 200 and messages_of(again)

    @pytest.mark.parametrize(
        "data",
        [
            {"role": Role.TECHNICIAN},  # a branch role without a branch
            {"role": Role.GROUP_ADMIN, "branch": "BRANCH"},  # a role not on a branch
            {"role": Role.PARTNER_REQUESTER},  # a partner without a company
            {"role": Role.TECHNICIAN, "branch": "BRANCH", "company": "COMPANY"},
        ],
    )
    def test_form_errors_grant_nothing(
        self, org: Org, people: People, data: dict[str, str]
    ) -> None:
        data = {
            key: {"BRANCH": str(org.a_mad.pk), "COMPANY": str(org.c.pk)}.get(value, value)
            for key, value in data.items()
        }
        data["idempotency_key"] = issue_key(people.group_admin.pk, "membership.grant")
        response = client_for(people.group_admin).post(
            reverse("org:grant_membership", args=[people.nobody.pk]), data, follow=True
        )
        assert response.status_code == 200 and messages_of(response)
        assert not Membership.objects.filter(user=people.nobody).exists()

    def test_service_refusals_are_reported(self, org: Org, people: People) -> None:
        # A partner membership on a company that does not employ them.
        response = client_for(people.group_admin).post(
            reverse("org:grant_membership", args=[people.nobody.pk]),
            {
                "role": Role.PARTNER_REQUESTER,
                "company": org.c.pk,
                "idempotency_key": issue_key(people.group_admin.pk, "membership.grant"),
            },
            follow=True,
        )
        assert messages_of(response) and not Membership.objects.filter(user=people.nobody)

    def test_employer(self, org: Org, people: People) -> None:
        client = client_for(people.group_admin)
        response = client.post(
            reverse("org:set_employer", args=[people.nobody.pk]), {"company": org.c.pk}
        )
        assert response.status_code == 302
        assert User.objects.get(pk=people.nobody.pk).employer_company == org.c
        refused = client.post(
            reverse("org:set_employer", args=[people.partner.pk]),
            {"company": org.a.pk},
            follow=True,
        )
        assert messages_of(refused)
        assert User.objects.get(pk=people.partner.pk).employer_company == org.c


class TestOffboardAndEmail:
    def test_offboard_needs_the_confirmation(self, people: People) -> None:
        client = client_for(people.group_admin)
        url = reverse("org:offboard", args=[people.tech_mad.pk])
        assert client.post(url).status_code == 302
        assert User.objects.get(pk=people.tech_mad.pk).is_active
        assert client.post(url, {"confirm": "on"}).status_code == 302
        tech = User.objects.get(pk=people.tech_mad.pk)
        assert not tech.is_active and tech.offboarded_at is not None
        again = client.post(url, {"confirm": "on"}, follow=True)
        assert messages_of(again)

    def test_email_change_request_and_revoke(self, people: People) -> None:
        client = client_for(people.group_admin)
        url = reverse("org:request_email_change", args=[people.tech_mad.pk])
        assert client.post(url, {"new_email": "not an email"}).status_code == 302
        assert not EmailChangeRequest.objects.exists()
        taken = client.post(url, {"new_email": people.mm_mad.email}, follow=True)
        assert messages_of(taken) and not EmailChangeRequest.objects.exists()
        assert client.post(url, {"new_email": "tech-new@example.org"}).status_code == 302
        change = EmailChangeRequest.objects.get()
        page = client.get(reverse("org:person", args=[people.tech_mad.pk]))
        assert b"tech-new@example.org" in page.content
        revoke = reverse("org:revoke_email_change", args=[change.pk])
        assert client.post(revoke, {"reason": "typo"}).status_code == 302
        change.refresh_from_db()
        assert change.revoke_reason == "typo"
        assert messages_of(client.post(revoke, follow=True))

    def test_staff_accounts_are_refused(self, people: People) -> None:
        client = client_for(people.group_admin)
        url = reverse("org:request_email_change", args=[people.operator.pk])
        assert client.post(url, {"new_email": "x@example.org"}).status_code == 403
        offboard = reverse("org:offboard", args=[people.operator.pk])
        assert client.post(offboard, {"confirm": "on"}).status_code == 403


class TestInvitations:
    def test_create_list_resend_revoke(self, org: Org, people: People) -> None:
        client = client_for(people.group_admin)
        form = client.get(reverse("org:new_invitation"))
        key = re.search(r'name="idempotency_key" value="([^"]+)"', form.content.decode())
        assert key is not None
        created = client.post(
            reverse("org:create_invitation"),
            {
                "email": "newbie@example.org",
                "role": Role.SITE_MANAGER,
                "branch": org.a_mad.pk,
                "project": org.hospital.pk,
                "idempotency_key": key.group(1),
            },
        )
        assert created.status_code == 302 and created["Location"] == reverse("org:invitations")
        invitation = Invitation.objects.get()
        assert invitation.grants["assignments"][0]["kind"] == "MANAGER"
        listed = client.get(reverse("org:invitations"))
        assert b"newbie@example.org" in listed.content
        partial = client.get(reverse("org:invitations"), {"q": "nobody"}, HTTP_HX_REQUEST="true")
        assert b"newbie@example.org" not in partial.content
        assert (
            client.post(reverse("org:resend_invitation", args=[invitation.pk])).status_code == 302
        )
        newer = Invitation.objects.get(revoked_at__isnull=True)
        assert newer.pk != invitation.pk
        assert messages_of(
            client.post(reverse("org:resend_invitation", args=[invitation.pk]), follow=True)
        )
        assert (
            client.post(
                reverse("org:revoke_invitation", args=[newer.pk]), {"reason": "x"}
            ).status_code
            == 302
        )
        assert not Invitation.objects.filter(revoked_at__isnull=True).exists()
        assert messages_of(
            client.post(reverse("org:revoke_invitation", args=[newer.pk]), follow=True)
        )

    def test_form_and_service_errors_rerender(self, org: Org, people: People) -> None:
        client = client_for(people.group_admin)
        key = issue_key(people.group_admin.pk, "invitation.create")
        project_for_a_tech = client.post(
            reverse("org:create_invitation"),
            {
                "email": "x@example.org",
                "role": Role.TECHNICIAN,
                "branch": org.a_mad.pk,
                "project": org.hospital.pk,
                "idempotency_key": key,
            },
        )
        assert project_for_a_tech.status_code == 200
        partner_elsewhere = client.post(
            reverse("org:create_invitation"),
            {
                "email": people.partner.email,
                "role": Role.PARTNER_REQUESTER,
                "employer_company": org.a.pk,
                "idempotency_key": key,
            },
        )
        assert partner_elsewhere.status_code == 200 and b"errorlist" in partner_elsewhere.content
        assert not Invitation.objects.exists()
