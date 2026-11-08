"""The lazy dialogs of the person and project pages (M8b.4): a role's validity and revocation,
a project assignment's validity and revocation.

The rows are on a paged list, so one shared dialog per kind of action is loaded for the row with
htmx: the GET answers the form's fragment to htmx (naming the dialog's content as the target) and
the whole page otherwise, with a Cancel that leads back. The POST answers by the contract of
``apps.core.responses``: 204 and ``HX-Redirect`` when saved, the form again (422) when not.
"""

import pytest
from django.test import Client
from django.urls import reverse

from apps.access.roles import Role
from apps.organizations.models import Membership
from apps.projects.models import ProjectAssignment
from tests.org import Org, People, assign, client_for, member

pytestmark = pytest.mark.django_db

HTMX = {"HX-Request": "true"}


def in_dialog(kind: str) -> dict[str, str]:
    return {**HTMX, "HX-Target": f"{kind}-dialog-content"}


@pytest.fixture
def admin(people: People) -> Client:
    return client_for(people.group_admin)


@pytest.fixture
def membership(org: Org, people: People) -> Membership:
    return Membership.objects.filter(user=people.tech_mad).first()  # type: ignore[return-value]


@pytest.fixture
def assignment(org: Org, people: People) -> ProjectAssignment:
    site = member(Role.SITE_MANAGER, email="site-2@example.org", branch=org.a_mad)
    return assign(site, org.sev_for_mad, "MANAGER")


class TestARole:
    def test_the_validity_form_is_a_fragment_for_the_dialog_and_a_page_otherwise(
        self, admin: Client, membership: Membership
    ) -> None:
        target = reverse("org:edit_membership_validity", args=[membership.pk])
        fragment = admin.get(target, headers=in_dialog("validity"))
        assert fragment.status_code == 200
        body = fragment.content.decode()
        assert body.lstrip().startswith("<form") and 'data-testid="validity-form"' in body
        assert "<html" not in body and "<dialog" not in body
        assert 'commandfor="validity-dialog"' in body  # Cancel closes the page's dialog
        assert "HX-Target" in fragment["Vary"] and "HX-Request" in fragment["Vary"]
        page = admin.get(target).content.decode()
        assert "<html" in page and 'data-testid="validity-form"' in page
        # Without the dialog Cancel is a link back to the person.
        assert f'href="{reverse("org:person", args=[membership.user_id])}"' in page

    def test_the_revoke_dialog_states_what_it_does(
        self, admin: Client, membership: Membership
    ) -> None:
        body = admin.get(
            reverse("org:confirm_revoke_membership", args=[membership.pk]),
            headers=in_dialog("revoke"),
        ).content.decode()
        assert "The person loses what this role gives, at once." in body

    def test_a_validity_that_is_saved_navigates_and_one_that_is_wrong_comes_back(
        self, admin: Client, membership: Membership
    ) -> None:
        target = reverse("org:membership_validity", args=[membership.pk])
        wrong = admin.post(
            target,
            {"valid_from": "2030-01-01T08:00", "valid_until": "2026-01-01T08:00"},
            headers=HTMX,
        )
        assert wrong.status_code == 422 and "data-error-summary" in wrong.content.decode()
        saved = admin.post(
            target,
            {"valid_from": "2026-01-01T08:00", "valid_until": "2030-01-01T08:00"},
            headers=HTMX,
        )
        assert saved.status_code == 204
        assert saved["HX-Redirect"] == reverse("org:person", args=[membership.user_id])
        membership.refresh_from_db()
        assert membership.valid_until is not None and membership.valid_until.year == 2030

    def test_a_revocation_from_the_dialog_navigates_and_a_second_one_is_refused_on_the_form(
        self, admin: Client, membership: Membership
    ) -> None:
        target = reverse("org:revoke_membership", args=[membership.pk])
        assert admin.post(target, headers=HTMX).status_code == 204
        membership.refresh_from_db()
        assert membership.revoked_at is not None
        again = admin.post(target, headers=HTMX)
        assert again.status_code == 422 and 'data-testid="revoke-form"' in again.content.decode()

    def test_the_person_page_has_the_openers_and_no_details_form(
        self, admin: Client, membership: Membership, people: People
    ) -> None:
        page = admin.get(reverse("org:person", args=[people.tech_mad.pk])).content.decode()
        assert 'data-dialog-open="validity-dialog"' in page and 'id="grant-dialog"' in page
        assert 'id="offboard-dialog"' in page
        assert "<details" not in page.split("<main", 1)[1].split("</main>", 1)[0]

    def test_a_site_manager_cannot_open_them(self, people: People, membership: Membership) -> None:
        client = client_for(people.site_manager)
        for name in ("edit_membership_validity", "confirm_revoke_membership"):
            assert client.get(reverse(f"org:{name}", args=[membership.pk])).status_code == 403


class TestAProjectAssignment:
    def test_the_forms_are_fragments_for_the_dialog(
        self, admin: Client, assignment: ProjectAssignment
    ) -> None:
        validity = admin.get(
            reverse("projects:edit_assignment_validity", args=[assignment.pk]),
            headers=in_dialog("validity"),
        )
        assert validity.status_code == 200 and "<html" not in validity.content.decode()
        revoke = admin.get(
            reverse("projects:confirm_revoke_assignment", args=[assignment.pk]),
            headers=in_dialog("revoke"),
        )
        assert "stops seeing this project" in revoke.content.decode()
        page = admin.get(reverse("projects:confirm_revoke_assignment", args=[assignment.pk]))
        assert "<html" in page.content.decode()

    def test_saved_revoked_and_refused(self, admin: Client, assignment: ProjectAssignment) -> None:
        validity = reverse("projects:assignment_validity", args=[assignment.pk])
        assert admin.post(validity, {}, headers=HTMX).status_code == 422
        saved = admin.post(
            validity,
            {"valid_from": "2026-01-01T08:00", "valid_until": "2030-01-01T08:00"},
            headers=HTMX,
        )
        assert saved.status_code == 204
        revoke = reverse("projects:revoke_assignment", args=[assignment.pk])
        assert admin.post(revoke, headers=HTMX).status_code == 204
        assert admin.post(revoke, headers=HTMX).status_code == 422
        assignment.refresh_from_db()
        assert assignment.revoked_at is not None

    def test_sharing_comes_back_in_its_dialog_when_the_form_is_empty(
        self, admin: Client, org: Org
    ) -> None:
        target = reverse("projects:assign", args=[org.sev_for_mad.pk])
        response = admin.post(target, {}, headers=HTMX)
        assert response.status_code == 422
        assert 'data-testid="share-form"' in response.content.decode()
        plain = admin.post(target, {})
        assert plain.status_code == 200 and 'id="share-dialog"' in plain.content.decode()
