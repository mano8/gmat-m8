"""The detail pages that listed every row now page (M7d.3): a project's assignments, a
person's roles, shared projects and invitations, and an object's audit history. Each section
pages on its own parameter, filters through its own prefixed form, and a screen never renders
more than its page size of rows."""

from datetime import timedelta
from typing import Any

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.access.roles import Role
from apps.accounts.models import Invitation
from apps.accounts.tokens import new_token
from apps.audit.models import AuditEvent
from apps.core.ids import new_id
from apps.projects.models import ProjectAssignment
from tests.org import Org, People, assign, make_user
from tests.org import client_for as signed_in
from tests.support import grant

pytestmark = pytest.mark.django_db

SIZE = 10  # the section page size; every screen is tested with SIZE + 1 rows


def client_for(user: Any) -> Any:
    return signed_in(user, HTTP_ACCEPT_LANGUAGE="en")


def get(client: Any, name: str, *args: Any, **query: Any) -> str:
    response = client.get(reverse(name, args=args), query)
    assert response.status_code == 200
    return str(response.content.decode())


class TestProjectAssignments:
    def shared(self, org: Org, count: int = SIZE + 1) -> None:
        for number in range(count):
            user = make_user(f"share{number}@example.org", full_name=f"Shared {number}")
            assign(user, org.hospital, "MANAGER")

    def test_a_page_never_shows_more_than_its_size(self, org: Org, people: People) -> None:
        self.shared(org)
        client = client_for(people.mm_mad)
        first = get(client, "projects:detail", org.hospital.pk)
        assert first.count('data-testid="project-assignment"') == SIZE
        second = get(client, "projects:detail", org.hospital.pk, assignments_page=2)
        total = ProjectAssignment.objects.filter(project=org.hospital).count()
        assert second.count('data-testid="project-assignment"') == total - SIZE
        assert 'id="assignments"' in second and "#assignments" in second

    def test_the_filters_narrow_and_the_pager_keeps_them(self, org: Org, people: People) -> None:
        self.shared(org, 3)
        revoked = assign(
            make_user("left@example.org", full_name="Left Behind"),
            org.hospital,
            "PARTNER",
            revoked_at=timezone.now(),
        )
        client = client_for(people.mm_mad)
        html = get(client, "projects:detail", org.hospital.pk, **{"assignments-state": "revoked"})
        assert "Left Behind" in html and "Shared 0" not in html  # by name (ADR-0029)
        assert "1 filter active" in html
        kind = get(client, "projects:detail", org.hospital.pk, **{"assignments-kind": "MANAGER"})
        assert "Shared 0" in kind and "Left Behind" not in kind
        none = get(client, "projects:detail", org.mad_hold.pk, **{"assignments-kind": "MANAGER"})
        assert "No assignment matches the filters." in none
        assert ProjectAssignment.objects.filter(pk=revoked.pk).exists()

    def test_a_reader_who_does_not_manage_sees_no_section(self, org: Org, people: People) -> None:
        self.shared(org, 2)
        html = get(client_for(people.site_manager), "projects:detail", org.hospital.pk)
        assert "Shared 0" not in html and 'id="assignments"' not in html

    def test_bad_parameters_fall_back(self, org: Org, people: People) -> None:
        client = client_for(people.mm_mad)
        html = get(
            client,
            "projects:detail",
            org.hospital.pk,
            assignments_page="x",
            assignments_per_page="100",
            **{"assignments-kind": "nope", "assignments-state": "nope"},
        )
        assert 'id="assignments"' in html


class TestPersonSections:
    def roles(self, people: People, org: Org, count: int = SIZE + 1) -> None:
        for _ in range(count - 1):
            grant(people.tech_mad, Role.TECHNICIAN, branch=org.a_mad)

    def test_roles_page_on_their_own_parameter(self, org: Org, people: People) -> None:
        self.roles(people, org)
        client = client_for(people.group_admin)
        first = get(client, "org:person", people.tech_mad.pk)
        assert first.count('data-testid="membership"') == SIZE
        assert 'href="?memberships_page=2#roles"' in first
        second = get(client, "org:person", people.tech_mad.pk, memberships_page=2)
        assert second.count('data-testid="membership"') == 1
        other = get(client, "org:person", people.tech_mad.pk, projects_page=9)
        assert other.count('data-testid="membership"') == SIZE  # another section's page

    def test_roles_filter_by_state(self, org: Org, people: People) -> None:
        grant(people.tech_mad, Role.TECHNICIAN, branch=org.a_sev, revoked_at=timezone.now())
        client = client_for(people.group_admin)
        revoked = get(client, "org:person", people.tech_mad.pk, **{"memberships-state": "revoked"})
        assert revoked.count('data-testid="membership"') == 1
        assert "1 filter active" in revoked
        active = get(client, "org:person", people.tech_mad.pk, **{"memberships-state": "active"})
        assert active.count('data-testid="membership"') == 1
        empty = get(client, "org:person", people.mm_mad.pk, **{"memberships-state": "revoked"})
        assert "No role matches the filters." in empty

    def test_shared_projects_page_and_filter(self, org: Org, people: People) -> None:
        for _ in range(SIZE + 1):
            assign(people.site_manager, org.hospital, "MANAGER", revoked_at=timezone.now())
        client = client_for(people.group_admin)
        first = get(client, "org:person", people.site_manager.pk)
        assert first.count('data-testid="shared-project"') == SIZE
        second = get(client, "org:person", people.site_manager.pk, projects_page=2)
        assert second.count('data-testid="shared-project"') == (
            ProjectAssignment.objects.filter(user=people.site_manager).count() - SIZE
        )
        by_kind = get(client, "org:person", people.site_manager.pk, **{"projects-kind": "PARTNER"})
        assert "No project matches the filters." in by_kind

    def test_invitations_page_filter_and_the_old_cut_is_gone(
        self, org: Org, people: People
    ) -> None:
        later = timezone.now() + timedelta(hours=24)
        for number in range(SIZE + 2):
            Invitation.objects.create(
                email=people.tech_mad.email,
                kind=Invitation.Kind.NEW_ACCOUNT,
                invited_by=people.group_admin,
                token_hash=new_token()[1],
                expires_at=later,
                revoked_at=timezone.now() if number == 0 else None,
            )
        client = client_for(people.group_admin)
        first = get(client, "org:person", people.tech_mad.pk)
        assert first.count('data-testid="person-invitation"') == SIZE
        second = get(client, "org:person", people.tech_mad.pk, invitations_page=2)
        assert second.count('data-testid="person-invitation"') == 2
        revoked = get(client, "org:person", people.tech_mad.pk, **{"invitations-status": "revoked"})
        assert revoked.count('data-testid="person-invitation"') == 1
        pending = get(client, "org:person", people.tech_mad.pk, **{"invitations-status": "pending"})
        assert pending.count('data-testid="person-invitation"') == SIZE
        accepted = get(
            client, "org:person", people.tech_mad.pk, **{"invitations-status": "accepted"}
        )
        assert "No invitation matches the filters." in accepted

    def test_a_non_administrator_still_gets_403(self, org: Org, people: People) -> None:
        response = client_for(people.mm_mad).get(reverse("org:person", args=[people.tech_mad.pk]))
        assert response.status_code == 403


class TestAuditHistory:
    OBJECT_TYPE = "material_requests.materialrequest"

    def history(self, org: Org, count: int = 51) -> Any:
        object_id = new_id()
        for number in range(count):
            AuditEvent.objects.create(
                branch_id=org.a_mad.pk,
                action=f"request.step{number}",
                actor_label="ana@example.org" if number % 2 else "bob@example.org",
                object_type=self.OBJECT_TYPE,
                object_id=object_id,
            )
        return object_id

    def url(self, object_id: Any) -> str:
        return reverse("audit:object", args=[self.OBJECT_TYPE, object_id])

    def test_a_page_never_shows_more_than_its_size(self, org: Org, people: People) -> None:
        object_id = self.history(org)
        client = client_for(people.mm_mad)
        first = client.get(self.url(object_id)).content.decode()
        assert first.count('data-testid="audit-row"') == 50
        assert "Showing 1\u201350 of 51" in first
        second = client.get(self.url(object_id), {"page": 2}).content.decode()
        assert second.count('data-testid="audit-row"') == 1
        small = client.get(self.url(object_id), {"per_page": 10}).content.decode()
        assert small.count('data-testid="audit-row"') == 10

    def test_oldest_first_by_default_and_sortable(self, org: Org, people: People) -> None:
        object_id = self.history(org, 12)
        client = client_for(people.mm_mad)
        ascending = client.get(self.url(object_id), {"per_page": 10}).content.decode()
        assert ascending.index("request.step0<") < ascending.index("request.step9<")
        descending = client.get(
            self.url(object_id), {"per_page": 10, "sort": "-time"}
        ).content.decode()
        assert descending.index("request.step11<") < descending.index("request.step3<")

    def test_text_and_date_filters_narrow_but_never_hide_the_object(
        self, org: Org, people: People
    ) -> None:
        object_id = self.history(org, 6)
        client = client_for(people.mm_mad)
        ana = client.get(self.url(object_id), {"q": "ana@"}).content.decode()
        assert ana.count('data-testid="audit-row"') == 3
        future = (timezone.now() + timedelta(days=2)).date().isoformat()
        later = client.get(self.url(object_id), {"since": future})
        assert later.status_code == 200
        assert "No event matches the filters." in later.content.decode()
        today = timezone.now().date().isoformat()
        until = client.get(self.url(object_id), {"since": today, "until": today})
        assert until.content.decode().count('data-testid="audit-row"') == 6

    def test_an_object_out_of_scope_is_still_a_404(self, org: Org, people: People) -> None:
        object_id = new_id()
        AuditEvent.objects.create(
            branch_id=org.a_sev.pk,
            action="request.submitted",
            actor_label="x@example.org",
            object_type=self.OBJECT_TYPE,
            object_id=object_id,
        )
        client = client_for(people.mm_mad)
        assert client.get(self.url(object_id), {"q": "request"}).status_code == 404

    def test_htmx_gets_the_results_only(self, org: Org, people: People) -> None:
        object_id = self.history(org, 3)
        response = client_for(people.mm_mad).get(
            self.url(object_id), headers={"HX-Request": "true"}
        )
        body = response.content.decode()
        assert "<html" not in body and 'id="history-results"' in body
