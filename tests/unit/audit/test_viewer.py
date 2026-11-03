"""The audit viewer (plan M7.3): the scoped list, its filters and one object's history."""

from datetime import date, timedelta
from uuid import UUID

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.access.context import AccessContext
from apps.access.exceptions import NotInScope
from apps.audit import selectors
from apps.audit.models import AuditEvent
from apps.core.ids import new_id
from tests.org import Org, People, client_for

pytestmark = pytest.mark.django_db

OBJECT = UUID("018f0000-0000-7000-8000-00000000a001")


def event(
    branch_id: UUID | None, action: str = "request.submitted", **fields: object
) -> AuditEvent:
    values: dict[str, object] = {
        "actor_label": "someone@example.org",
        "object_type": "material_requests.materialrequest",
        "object_id": new_id(),
    }
    return AuditEvent.objects.create(branch_id=branch_id, action=action, **(values | fields))


class TestScope:
    def test_each_role_reads_its_branches_events_only(self, org: Org, people: People) -> None:
        mad = event(org.a_mad.pk)
        sev = event(org.a_sev.pk)
        none = event(None, "org.updated")
        everything = {mad.pk, sev.pk, none.pk}

        def ids(user: object) -> set[UUID]:
            return {e.pk for e in selectors.events(AccessContext.for_user(user))}  # type: ignore[arg-type]

        assert everything <= ids(people.group_admin)
        assert ids(people.mm_mad) == {mad.pk}
        assert ids(people.mm_sev) == {sev.pk}

    @pytest.mark.parametrize("who", ["wo_mad", "tech_mad", "site_manager", "partner", "nobody"])
    def test_roles_without_the_capability_read_nothing(
        self, org: Org, people: People, who: str
    ) -> None:
        event(org.a_mad.pk)
        ctx = AccessContext.for_user(getattr(people, who))
        assert not selectors.can_view(ctx)
        assert not selectors.events(ctx).exists()

    def test_a_revoked_membership_reads_nothing(self, org: Org, people: People) -> None:
        event(org.a_mad.pk)
        assert not selectors.events(AccessContext.for_user(people.revoked)).exists()

    def test_the_payload_and_the_address_are_not_selected(self, org: Org, people: People) -> None:
        event(org.a_mad.pk, data={"unit_price": "98765.4321"}, ip="203.0.113.9")
        found = selectors.events(AccessContext.for_user(people.mm_mad)).get()
        assert "98765" not in str(found.__dict__)
        assert "203.0.113.9" not in str(found.__dict__)


class TestFilters:
    def test_text_type_and_dates_narrow_the_list(self, org: Org, people: People) -> None:
        ctx = AccessContext.for_user(people.mm_mad)
        old = event(
            org.a_mad.pk,
            "po.issued",
            object_type="purchasing.purchaseorder",
            occurred_at=timezone.now() - timedelta(days=40),
        )
        recent = event(org.a_mad.pk, "request.approved", via="cli:import")

        def found(**narrow: object) -> set[UUID]:
            return {e.pk for e in selectors.events(ctx, selectors.EventFilter(**narrow))}  # type: ignore[arg-type]

        assert found(q="approved") == {recent.pk}
        assert found(q="cli:import") == {recent.pk}
        assert found(object_type="purchasing.purchaseorder") == {old.pk}
        assert found(since=date.today()) == {recent.pk}
        assert found(until=date.today() - timedelta(days=30)) == {old.pk}
        assert found(since=date.today(), until=date.today()) == {recent.pk}

    def test_object_types_come_from_the_visible_events(self, org: Org, people: People) -> None:
        event(org.a_mad.pk, object_type="a.one")
        event(org.a_sev.pk, object_type="b.two")
        event(org.a_mad.pk, object_type="")
        assert selectors.object_types(AccessContext.for_user(people.mm_mad)) == ["a.one"]


class TestHistory:
    def test_the_events_of_one_object_oldest_first(self, org: Org, people: People) -> None:
        first = event(org.a_mad.pk, "request.created", object_id=OBJECT)
        second = event(org.a_mad.pk, "request.submitted", object_id=OBJECT)
        event(org.a_mad.pk, "request.created")
        rows = selectors.object_history(
            AccessContext.for_user(people.mm_mad), "material_requests.materialrequest", OBJECT
        )
        assert [e.pk for e in rows] == [first.pk, second.pk]

    def test_an_object_outside_the_scope_is_not_found(self, org: Org, people: People) -> None:
        event(org.a_sev.pk, object_id=OBJECT)
        with pytest.raises(NotInScope):
            selectors.object_history(
                AccessContext.for_user(people.mm_mad), "material_requests.materialrequest", OBJECT
            )


class TestViews:
    def test_the_list_filters_and_links_to_the_history(self, org: Org, people: People) -> None:
        event(org.a_mad.pk, "request.approved", object_id=OBJECT)
        event(org.a_mad.pk, "po.issued", object_type="purchasing.purchaseorder")
        client = client_for(people.mm_mad)
        html = client.get(reverse("audit:list"), {"q": "approved"}).content.decode()
        assert html.count('data-testid="audit-row"') == 1
        assert reverse("audit:object", args=["material_requests.materialrequest", OBJECT]) in html

    def test_a_bad_filter_is_ignored_and_a_fragment_has_no_layout(
        self, org: Org, people: People
    ) -> None:
        event(org.a_mad.pk)
        client = client_for(people.mm_mad)
        response = client.get(reverse("audit:list"), {"since": "yesterday"})
        assert response.status_code == 200
        assert response.content.decode().count('data-testid="audit-row"') == 1
        fragment = client.get(reverse("audit:list"), headers={"HX-Request": "true"})
        assert "<html" not in fragment.content.decode()

    def test_the_list_pages_keep_the_filters(self, org: Org, people: People) -> None:
        for _ in range(selectors.DEFAULT_PER_PAGE + 1):
            event(org.a_mad.pk)
        html = (
            client_for(people.mm_mad)
            .get(reverse("audit:list"), {"q": "request", "since": "2020-01-01"})
            .content.decode()
        )
        link = next(part for part in html.split('href="') if "page=2" in part[:80])
        assert all(kept in link.split('"')[0] for kept in ("q=request", "since=2020-01-01"))

    def test_the_empty_state(self, org: Org, people: People) -> None:
        html = client_for(people.mm_mad).get(reverse("audit:list")).content.decode()
        assert 'data-testid="audit-row"' not in html

    def test_the_history_page_and_its_404(self, org: Org, people: People) -> None:
        event(org.a_mad.pk, object_id=OBJECT)
        url = reverse("audit:object", args=["material_requests.materialrequest", OBJECT])
        assert client_for(people.mm_mad).get(url).status_code == 200
        assert client_for(people.mm_sev).get(url).status_code == 404
        assert client_for(people.wo_mad, mfa=False).get(url).status_code == 403
