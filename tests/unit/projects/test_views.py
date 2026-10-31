"""Project screens (M2.6): scope of the list and detail, create, edit and assignments."""

import re

import pytest
from django.test import Client
from django.urls import reverse

from apps.access.roles import Role
from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.core.idempotency import issue_key
from apps.projects.models import Project, ProjectAssignment
from tests.org import Org, People, client_for, member

pytestmark = pytest.mark.django_db


def codes(response: object) -> set[str]:
    return set(re.findall(r">([A-Z0-9-]+)</a>", response.content.decode()))  # type: ignore[attr-defined]


def form_key(response: object) -> str:
    match = re.search(r'name="idempotency_key" value="([^"]+)"', response.content.decode())  # type: ignore[attr-defined]
    assert match is not None
    return match.group(1)


class TestListAndDetail:
    def test_each_role_lists_its_scope(self, org: Org, people: People) -> None:
        expected = {
            "group_admin": {"HOSP-X", "SEV-1", "SEV-2", "MAD-1"},
            "mm_mad": {"HOSP-X", "SEV-2", "MAD-1"},  # A-MAD, B-MAD, and A-MAD buys for SEV-2
            "mm_sev": {"SEV-1", "SEV-2"},
            "tech_mad": {"HOSP-X", "SEV-2"},  # active projects only
            "site_manager": {"HOSP-X"},
            "partner": {"HOSP-X"},
        }
        for name, visible in expected.items():
            response = client_for(getattr(people, name)).get(reverse("projects:list"))
            assert response.status_code == 200, name
            assert codes(response) == visible, name

    def test_partners_cannot_open_projects_not_shared_with_them(
        self, org: Org, people: People
    ) -> None:
        client = client_for(people.partner)
        for project in (org.sev_works, org.sev_for_mad, org.mad_hold):
            assert client.get(reverse("projects:detail", args=[project.pk])).status_code == 404
        shared = client.get(reverse("projects:detail", args=[org.hospital.pk]))
        assert shared.status_code == 200
        # Who else the project is shared with stays hidden.
        assert people.site_manager.email.encode() not in shared.content
        assert b"projects:assign" not in shared.content and b"/assignments/" not in shared.content

    def test_filters_and_the_htmx_partial(self, org: Org, people: People) -> None:
        client = client_for(people.group_admin)
        assert codes(client.get(reverse("projects:list"), {"q": "sev"})) == {"SEV-1", "SEV-2"}
        assert codes(client.get(reverse("projects:list"), {"status": "ON_HOLD"})) == {"MAD-1"}
        partial = client.get(reverse("projects:list"), {"q": "hosp"}, HTTP_HX_REQUEST="true")
        assert partial.status_code == 200
        assert partial.content.lstrip().startswith(b'<div id="project-results">')
        assert b"<html" not in partial.content
        empty = client.get(reverse("projects:list"), {"q": "nothing-matches"})
        assert codes(empty) == set()

    def test_managers_see_the_assignments(self, org: Org, people: People) -> None:
        response = client_for(people.mm_mad).get(reverse("projects:detail", args=[org.hospital.pk]))
        assert people.site_manager.email.encode() in response.content
        assert reverse("projects:assign", args=[org.hospital.pk]).encode() in response.content


class TestCreateAndEdit:
    def test_create(self, org: Org, people: People) -> None:
        client = client_for(people.mm_mad)
        key = form_key(client.get(reverse("projects:new")))
        data = {
            "code": "MAD-2",
            "name": "Nueva obra",
            "branch": org.b_mad.pk,
            "procurement_branch": "",
            "idempotency_key": key,
        }
        response = client.post(reverse("projects:create"), data)
        project = Project.objects.get(code="MAD-2")
        assert response.status_code == 302
        assert response["Location"] == reverse("projects:detail", args=[project.pk])
        assert project.procurement_branch == org.a_mad  # B-MAD is managed by A-MAD
        # The same form posted again (a double click) answers with the same project.
        again = client.post(reverse("projects:create"), data)
        assert again["Location"] == response["Location"]
        assert Project.objects.filter(code="MAD-2").count() == 1

    def test_create_errors(self, org: Org, people: People) -> None:
        client = client_for(people.mm_sev)
        key = issue_key(people.mm_sev.pk, "project.create")
        outside = client.post(
            reverse("projects:create"),
            {"code": "X-1", "name": "X", "branch": org.a_mad.pk, "idempotency_key": key},
        )
        assert outside.status_code == 200 and not Project.objects.filter(code="X-1")
        duplicate = client.post(
            reverse("projects:create"),
            {"code": "SEV-1", "name": "X", "branch": org.a_sev.pk, "idempotency_key": key},
        )
        assert duplicate.status_code == 200 and b"errorlist" in duplicate.content
        foreign = client.post(
            reverse("projects:create"),
            {
                "code": "X-2",
                "name": "X",
                "branch": org.a_sev.pk,
                "idempotency_key": issue_key(people.mm_mad.pk, "project.create"),
            },
        )
        assert foreign.status_code == 400

    def test_edit(self, org: Org, people: People) -> None:
        client = client_for(people.mm_mad)
        page = client.get(reverse("projects:edit", args=[org.hospital.pk]))
        assert page.status_code == 200 and b"HOSP-X" in page.content
        response = client.post(
            reverse("projects:update", args=[org.hospital.pk]),
            {"name": "Hospital X2", "branch": org.b_mad.pk, "status": "ON_HOLD"},
        )
        assert response.status_code == 302
        org.hospital.refresh_from_db()
        assert (org.hospital.name, org.hospital.status) == ("Hospital X2", "ON_HOLD")
        assert AuditEvent.objects.filter(action="project.updated", object_id=org.hospital.pk)
        bad_dates = client.post(
            reverse("projects:update", args=[org.hospital.pk]),
            {
                "name": "H",
                "branch": org.b_mad.pk,
                "status": "ACTIVE",
                "start_date": "2026-12-01",
                "end_date": "2026-11-01",
            },
        )
        assert bad_dates.status_code == 200 and b"errorlist" in bad_dates.content

    def test_edit_needs_the_project_in_scope(self, org: Org, people: People) -> None:
        assert (
            client_for(people.mm_sev)
            .get(reverse("projects:edit", args=[org.hospital.pk]))
            .status_code
            == 404
        )
        assert (
            client_for(people.tech_mad)
            .get(reverse("projects:edit", args=[org.hospital.pk]))
            .status_code
            == 403
        )
        assert (
            client_for(people.mixed)
            .get(reverse("projects:edit", args=[org.hospital.pk]))
            .status_code
            == 403
        )


class TestAssignments:
    def test_share_change_and_revoke(self, org: Org, people: People) -> None:
        site = member(Role.SITE_MANAGER, email="site-2@example.org", branch=org.a_mad)
        client = client_for(people.mm_mad)
        key = form_key(client.get(reverse("projects:detail", args=[org.sev_for_mad.pk])))
        response = client.post(
            reverse("projects:assign", args=[org.sev_for_mad.pk]),
            {"kind": "MANAGER", "user": site.pk, "idempotency_key": key},
        )
        assert response.status_code == 302
        assignment = ProjectAssignment.objects.get(user=site, project=org.sev_for_mad)
        validity = client.post(
            reverse("projects:assignment_validity", args=[assignment.pk]),
            {"valid_from": "2026-01-01T08:00", "valid_until": "2030-01-01T08:00"},
        )
        assert validity.status_code == 302
        assignment.refresh_from_db()
        assert assignment.valid_until is not None and assignment.valid_until.year == 2030
        wrong = client.post(
            reverse("projects:assignment_validity", args=[assignment.pk]),
            {"valid_from": "2030-01-01T08:00", "valid_until": "2026-01-01T08:00"},
        )
        assert wrong.status_code == 302
        revoke = client.post(reverse("projects:revoke_assignment", args=[assignment.pk]))
        assert revoke.status_code == 302
        assignment.refresh_from_db()
        assert assignment.revoked_at is not None
        # Revoking again only reports it.
        assert (
            client.post(reverse("projects:revoke_assignment", args=[assignment.pk])).status_code
            == 302
        )

    def test_only_people_with_the_matching_role(self, org: Org, people: People) -> None:
        client = client_for(people.mm_mad)
        response = client.post(
            reverse("projects:assign", args=[org.sev_for_mad.pk]),
            {
                "kind": "PARTNER",
                "user": people.site_manager.pk,
                "idempotency_key": issue_key(people.mm_mad.pk, "project_assignment.grant"),
            },
        )
        assert response.status_code == 200 and b"errorlist" in response.content
        technician = client.post(
            reverse("projects:assign", args=[org.sev_for_mad.pk]),
            {
                "kind": "MANAGER",
                "user": people.tech_mad.pk,
                "idempotency_key": issue_key(people.mm_mad.pk, "project_assignment.grant"),
            },
        )
        assert technician.status_code == 200
        assert not ProjectAssignment.objects.filter(project=org.sev_for_mad).exists()

    def test_assignments_of_foreign_projects_are_not_found(self, org: Org, people: People) -> None:
        assignment = ProjectAssignment.objects.get(user=people.site_manager)
        client = client_for(people.mm_sev)
        assert (
            client.post(reverse("projects:revoke_assignment", args=[assignment.pk])).status_code
            == 404
        )
        assert (
            client.post(reverse("projects:assignment_validity", args=[assignment.pk])).status_code
            == 404
        )


def test_signed_out_users_go_to_the_login(org: Org) -> None:
    response = Client().get(reverse("projects:list"))
    assert response.status_code == 302 and response["Location"].startswith(reverse("account_login"))


def test_nobody_without_a_project_role_sees_the_list(people: People) -> None:
    user: User = people.nobody
    assert client_for(user, mfa=False).get(reverse("projects:list")).status_code == 403
