"""The reads behind the handover screens (M6.3): the list and page of handovers, the projects,
lines, warehouses and people of the handover form."""

from decimal import Decimal

import pytest

from apps.access.context import AccessContext
from apps.access.exceptions import NotInScope
from apps.accounts.models import User
from apps.handovers import selectors
from apps.handovers.services import Receiver
from apps.locations.models import Location
from apps.material_requests.models import MaterialRequest
from apps.organizations.models import Membership
from apps.projects.models import Project, ProjectAssignment
from tests.org import Org, People
from tests.unit.handovers.conftest import HandOver
from tests.unit.purchasing.conftest import Buy, MakeLine
from tests.unit.receiving.conftest import Receive

pytestmark = pytest.mark.django_db

D = Decimal


def ctx(user: User) -> AccessContext:
    return AccessContext.for_user(user)


class TestHandovers:
    def test_scope_and_filters(
        self,
        org: Org,
        people: People,
        make_line: MakeLine,
        hand_over: HandOver,
        buy: Buy,
        receive: Receive,
    ) -> None:
        line = make_line("10", sourcing="STOCK")
        stock = hand_over((line, "4"))
        order = buy((make_line("5"), "5"))
        direct = receive(order, "5", at=org.worksite(org.hospital)).direct_handover
        everything = {stock, direct}
        assert set(selectors.handovers(ctx(people.mm_mad))) == everything
        assert set(selectors.handovers(ctx(people.site_manager))) == everything
        assert set(selectors.handovers(ctx(people.tech_mad))) == set()
        assert set(selectors.handovers(ctx(people.mm_mad), kind="FROM_WAREHOUSE")) == {stock}
        assert set(selectors.handovers(ctx(people.mm_mad), kind="DIRECT_RECEIPT")) == {direct}
        assert set(selectors.handovers(ctx(people.mm_mad), status="VOIDED")) == set()
        assert set(selectors.handovers(ctx(people.mm_mad), status="POSTED")) == everything
        by_project = selectors.handovers(ctx(people.mm_mad), project_id=org.hospital.pk)
        assert set(by_project) == everything
        assert set(selectors.handovers(ctx(people.mm_mad), project_id=org.sev_for_mad.pk)) == set()
        for query in (stock.number, org.hospital.code.lower(), org.hospital.name[:4]):
            assert stock in selectors.handovers(ctx(people.mm_mad), q=query)
        typed = hand_over((line, "1"), receiver=Receiver(name="Pat Taker"))
        assert list(selectors.handovers(ctx(people.mm_mad), q="pat tak")) == [typed]

    def test_the_projects_to_filter_by(self, org: Org, people: People) -> None:
        assert [p.pk for p in selectors.list_projects(ctx(people.site_manager))] == [
            org.hospital.pk
        ]

    def test_one_handover_or_404(
        self, org: Org, people: People, make_line: MakeLine, hand_over: HandOver
    ) -> None:
        stock = hand_over((make_line("10", sourcing="STOCK"), "4"))
        assert selectors.handover(ctx(people.site_manager), stock.pk) == stock
        for user in (people.tech_mad, people.partner, people.mm_sev):
            with pytest.raises(NotInScope):
                selectors.handover(ctx(user), stock.pk)

    def test_who_may_post_or_void(
        self,
        org: Org,
        people: People,
        make_line: MakeLine,
        hand_over: HandOver,
        buy: Buy,
        receive: Receive,
    ) -> None:
        stock = hand_over((make_line("10", sourcing="STOCK"), "4"))
        direct = receive(
            buy((make_line("5"), "5")), "5", at=org.worksite(org.hospital)
        ).direct_handover
        assert selectors.can_post(ctx(people.wo_mad)) and not selectors.can_post(
            ctx(people.site_manager)
        )
        assert selectors.can_void(ctx(people.mm_mad), stock)
        assert not selectors.can_void(ctx(people.wo_mad), stock)
        assert not selectors.can_void(ctx(people.mm_mad), direct)  # goes with its receipt

    def test_the_lines(
        self, org: Org, people: People, make_line: MakeLine, hand_over: HandOver
    ) -> None:
        first, second = make_line("10", sourcing="STOCK"), make_line("6", sourcing="STOCK")
        stock = hand_over((first, "4"), (second, "2"))
        lines = selectors.handover_lines(ctx(people.site_manager), stock)
        assert {(row.request_line_id, row.qty) for row in lines} == {
            (first.pk, D(4)),
            (second.pk, D(2)),
        }


class TestTheForm:
    def test_projects_with_a_live_line_to_hand_over(
        self, org: Org, people: People, make_line: MakeLine
    ) -> None:
        make_line("10", sourcing="STOCK")
        assert [p.pk for p in selectors.posting_projects(ctx(people.wo_mad))] == [org.hospital.pk]
        assert list(selectors.posting_projects(ctx(people.site_manager))) == []
        assert selectors.posting_project(ctx(people.wo_mad), org.hospital.pk) == org.hospital
        with pytest.raises(NotInScope):
            selectors.posting_project(ctx(people.site_manager), org.hospital.pk)
        with pytest.raises(NotInScope):
            selectors.posting_project(ctx(people.wo_mad), org.sev_for_mad.pk)
        Project.objects.filter(pk=org.hospital.pk).update(status=Project.Status.ON_HOLD)
        assert list(selectors.posting_projects(ctx(people.wo_mad))) == []

    def test_a_request_is_found_when_approved_and_in_scope(
        self, org: Org, people: People, make_line: MakeLine
    ) -> None:
        line = make_line("10", sourcing="STOCK")
        found = selectors.posting_request(ctx(people.wo_mad), line.request_id)
        assert found == line.request
        for user in (people.site_manager, people.mm_sev):
            with pytest.raises(NotInScope):
                selectors.posting_request(ctx(user), line.request_id)
        closed = make_line("10", sourcing="STOCK", status="CLOSED")
        with pytest.raises(NotInScope):
            selectors.posting_request(ctx(people.wo_mad), closed.request_id)

    def test_open_lines_with_what_is_left(
        self, org: Org, people: People, make_line: MakeLine, hand_over: HandOver
    ) -> None:
        part, full, other = (make_line(q, sourcing="STOCK") for q in ("10", "3", "2"))
        hand_over((part, "4"), (full, "3"))
        rows = selectors.open_lines(ctx(people.wo_mad), org.hospital)
        assert {(r.line.pk, r.delivered, r.outstanding) for r in rows} == {
            (part.pk, D(4), D(6)),
            (other.pk, D(0), D(2)),
        }
        asked = selectors.open_lines(ctx(people.wo_mad), org.hospital, request=other.request)
        assert [r.line.pk for r in asked] == [other.pk]

    def test_only_the_projects_procurement_branch_without_a_request(
        self, org: Org, people: People, make_line: MakeLine
    ) -> None:
        line = make_line("10", sourcing="STOCK")
        MaterialRequest.objects.filter(pk=line.request_id).update(procurement_branch=org.a_sev)
        assert selectors.open_lines(ctx(people.mm_mad), org.hospital) == []

    def test_the_warehouses_of_the_branch_scope(self, org: Org, people: People) -> None:
        places = selectors.sources(ctx(people.wo_mad), org.a_mad.pk)
        assert {place.pk for place in places} == {
            org.warehouses["A Madrid"].pk,
            org.warehouses["B Madrid"].pk,
        }
        assert list(selectors.sources(ctx(people.site_manager), org.a_mad.pk)) == []
        Location.objects.filter(pk=org.warehouses["B Madrid"].pk).update(is_active=False)
        assert [p.pk for p in selectors.sources(ctx(people.wo_mad), org.a_mad.pk)] == [
            org.warehouses["A Madrid"].pk
        ]

    def test_the_people_who_may_take_the_material(self, org: Org, people: People) -> None:
        hospital = Project.objects.select_related("branch").get(pk=org.hospital.pk)
        found = selectors.receivers(hospital)
        assert people.partner in found and people.site_manager in found
        assert people.revoked_assignment not in found
        assert people.inactive_partner not in found  # their company is inactive (S9)
        assert people.nobody not in found
        assert people.mm_sev not in found
        ProjectAssignment.objects.filter(user=people.partner).update(revoked_at=hospital.created_at)
        assert people.partner not in selectors.receivers(hospital)
        User.objects.filter(pk=people.site_manager.pk).update(is_active=False)
        assert people.site_manager not in selectors.receivers(hospital)

    def test_a_branch_that_is_managed_also_counts_its_managers_staff(
        self, org: Org, people: People
    ) -> None:
        sev = Project.objects.select_related("branch").get(pk=org.sev_for_mad.pk)
        assert people.mm_sev in selectors.receivers(sev)
        assert Membership.objects.filter(user=people.mm_sev).exists()
