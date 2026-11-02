"""Every list runs on the toolkit (M7d.2): bad parameters fall back, every sort key answers, an
htmx request gets the results and a history restore the page; plus the filters M7d.2 added to
the people, invitation, project and audit lists."""

from datetime import timedelta
from typing import Any

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.access.context import AccessContext
from apps.accounts import selectors as account_selectors
from apps.accounts.models import Invitation
from apps.audit import selectors as audit_selectors
from apps.audit.models import AuditEvent
from apps.catalog import selectors as catalog_selectors
from apps.core.ids import new_id
from apps.handovers import selectors as handover_selectors
from apps.material_requests import selectors as request_selectors
from apps.notifications.models import Status
from apps.projects import selectors as project_selectors
from apps.purchasing import selectors as purchasing_selectors
from apps.receiving import selectors as receiving_selectors
from apps.suppliers import selectors as supplier_selectors
from tests.org import Org, People, client_for
from tests.unit.organizations.test_delivery import invite, outbox_of, set_status

pytestmark = pytest.mark.django_db

# url name → (who reads it, sort keys)
LISTS: dict[str, tuple[str, dict[str, Any]]] = {
    "catalog:list": ("mm_mad", catalog_selectors.MATERIAL_SORTS),
    "suppliers:list": ("mm_mad", supplier_selectors.SUPPLIER_SORTS),
    "projects:list": ("mm_mad", project_selectors.PROJECT_SORTS),
    "org:people": ("group_admin", account_selectors.PEOPLE_SORTS),
    "org:invitations": ("group_admin", account_selectors.INVITATION_SORTS),
    "requests:list": ("tech_mad", request_selectors.MY_REQUEST_SORTS),
    "requests:new": ("tech_mad", request_selectors.PROJECT_SORTS),
    "requests:inbox": ("mm_mad", request_selectors.INBOX_SORTS),
    "purchasing:orders": ("mm_mad", purchasing_selectors.ORDER_SORTS),
    "receiving:list": ("mm_mad", receiving_selectors.RECEIPT_SORTS),
    "receiving:orders": ("mm_mad", receiving_selectors.ORDER_SORTS),
    "receiving:projects": ("site_manager", receiving_selectors.PROJECT_SORTS),
    "handovers:list": ("mm_mad", handover_selectors.HANDOVER_SORTS),
    "audit:list": ("mm_mad", audit_selectors.EVENT_SORTS),
}


@pytest.mark.parametrize("name", sorted(LISTS))
class TestEveryList:
    def test_invalid_parameters_fall_back(self, org: Org, people: People, name: str) -> None:
        who, _ = LISTS[name]
        client = client_for(getattr(people, who))
        for query in (
            {},
            {"page": "0", "per_page": "7", "sort": "nope"},
            {"page": "999", "per_page": "10000"},
            {"page": "x", "per_page": "100", "sort": "-"},
        ):
            assert client.get(reverse(name), query).status_code == 200

    def test_every_sort_key_answers(self, org: Org, people: People, name: str) -> None:
        who, sorts = LISTS[name]
        client = client_for(getattr(people, who))
        for key in sorts:
            for sort in (key, f"-{key}"):
                response = client.get(reverse(name), {"sort": sort, "per_page": "10"})
                assert response.status_code == 200
                assert f'<option value="{sort}" selected>' in response.content.decode()

    def test_htmx_gets_the_results_and_a_history_restore_the_page(
        self, org: Org, people: People, name: str
    ) -> None:
        who, _ = LISTS[name]
        client = client_for(getattr(people, who))
        fragment = client.get(reverse(name), headers={"HX-Request": "true"})
        assert "<html" not in fragment.content.decode()
        assert "HX-Request" in fragment.headers["Vary"]
        restored = client.get(
            reverse(name), headers={"HX-Request": "true", "HX-History-Restore-Request": "true"}
        )
        assert "<html" in restored.content.decode()


class TestPaging:
    def test_page_size_and_page_round_trip_through_the_links(
        self, org: Org, people: People
    ) -> None:
        for _ in range(12):
            AuditEvent.objects.create(
                branch_id=org.a_mad.pk,
                action="request.submitted",
                actor_label="someone@example.org",
                object_type="material_requests.materialrequest",
                object_id=new_id(),
            )
        client = client_for(people.mm_mad)
        html = client.get(
            reverse("audit:list"), {"per_page": "10", "sort": "-time"}
        ).content.decode()
        assert html.count('data-testid="audit-row"') == 10
        assert "page=2" in html
        assert "per_page=10" in html
        second = client.get(reverse("audit:list"), {"per_page": "10", "page": "2"})
        assert second.content.decode().count('data-testid="audit-row"') == 2

    def test_an_actor_filter_narrows_the_scoped_events(self, org: Org, people: People) -> None:
        for actor in ("ana@example.org", "bob@example.org"):
            AuditEvent.objects.create(
                branch_id=org.a_mad.pk,
                action="request.submitted",
                actor_label=actor,
                object_type="x.y",
                object_id=new_id(),
            )
        AuditEvent.objects.create(
            branch_id=org.a_sev.pk,
            action="request.submitted",
            actor_label="ana@sev.example.org",
            object_type="x.y",
            object_id=new_id(),
        )
        ctx = AccessContext.for_user(people.mm_mad)
        narrow = audit_selectors.EventFilter(actor="ANA")
        assert [e.actor_label for e in audit_selectors.events(ctx, narrow)] == ["ana@example.org"]


class TestProjectsAndPeople:
    def test_branch_filter_of_projects(self, org: Org, people: People) -> None:
        ctx = AccessContext.for_user(people.group_admin)
        found = project_selectors.projects(ctx, branch_id=org.a_sev.pk)
        assert org.sev_works in found
        assert org.hospital not in found

    def test_a_branch_outside_the_scope_is_not_a_choice(self, org: Org, people: People) -> None:
        response = client_for(people.mm_sev).get(
            reverse("projects:list"), {"branch": str(org.a_mad.pk)}
        )
        assert response.status_code == 200
        assert org.hospital.code not in response.content.decode()

    def test_role_and_branch_filter_of_people(self, org: Org, people: People) -> None:
        ctx = AccessContext.for_user(people.group_admin)
        technicians = account_selectors.people(ctx, role="TECHNICIAN")
        assert people.tech_mad in technicians
        assert people.mm_mad not in technicians
        at_sev = account_selectors.people(ctx, branch_id=org.a_sev.pk)
        assert people.mm_sev in at_sev
        assert people.tech_mad not in at_sev
        both = account_selectors.people(ctx, role="MATERIAL_MANAGER", branch_id=org.a_mad.pk)
        assert people.mm_mad in both
        assert people.mm_sev not in both

    def test_a_revoked_membership_does_not_match_a_role_filter(
        self, org: Org, people: People
    ) -> None:
        ctx = AccessContext.for_user(people.group_admin)
        assert people.revoked not in account_selectors.people(ctx, role="MATERIAL_MANAGER")

    def test_people_filters_are_for_administrators(self, org: Org, people: People) -> None:
        ctx = AccessContext.for_user(people.mm_mad)
        assert not account_selectors.people(ctx, role="TECHNICIAN").exists()
        assert not account_selectors.invitations(ctx, state="valid").exists()


class TestInvitations:
    def test_validity_and_delivery(self, org: Org, people: People) -> None:
        client = client_for(people.group_admin)
        fresh = invite(client, org, people, "fresh@example.org")
        old = invite(client, org, people, "old@example.org")
        Invitation.objects.filter(pk=old.pk).update(expires_at=timezone.now() - timedelta(days=1))
        ctx = AccessContext.for_user(people.group_admin)
        assert list(account_selectors.invitations(ctx, state="expired")) == [old]
        assert list(account_selectors.invitations(ctx, state="valid")) == [fresh]
        set_status(outbox_of(old), Status.FAILED)
        assert list(account_selectors.invitations(ctx, delivery=Status.FAILED)) == [old]
        assert list(account_selectors.invitations(ctx, delivery=Status.QUEUED)) == [fresh]
        assert not account_selectors.invitations(ctx, delivery=account_selectors.NO_EMAIL)
        outbox_of(fresh).delete()
        assert list(account_selectors.invitations(ctx, delivery=account_selectors.NO_EMAIL)) == [
            fresh
        ]

    def test_the_page_shows_the_filtered_invitation_with_its_status(
        self, org: Org, people: People
    ) -> None:
        client = client_for(people.group_admin)
        invite(client, org, people, "shown@example.org")
        html = client.get(
            reverse("org:invitations"), {"delivery": Status.QUEUED, "sort": "-expires"}
        ).content.decode()
        assert "shown@example.org" in html
        other = client.get(reverse("org:invitations"), {"delivery": Status.SENT}).content.decode()
        assert "shown@example.org" not in other
