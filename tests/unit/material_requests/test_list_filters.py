"""The "My requests" filters and the inbox sort keys (M7d.2)."""

import pytest
from django.urls import reverse

from apps.access.context import AccessContext
from apps.material_requests import selectors
from apps.material_requests.models import MaterialRequest
from tests.org import Org, People, client_for
from tests.unit.material_requests.conftest import MakeRequest

pytestmark = pytest.mark.django_db

Status = MaterialRequest.Status


def test_my_requests_by_number_and_project(
    org: Org, people: People, make_request: MakeRequest
) -> None:
    mine = make_request(Status.SUBMITTED)
    other_project = make_request(Status.SUBMITTED, project=org.sev_for_mad)
    ctx = AccessContext.for_user(people.tech_mad)
    assert set(selectors.my_requests(ctx)) == {mine, other_project}
    assert list(selectors.my_requests(ctx, q=f" {str(mine.number).lower()} ")) == [mine]
    assert list(selectors.my_requests(ctx, project_id=org.sev_for_mad.pk)) == [other_project]
    assert not selectors.my_requests(ctx, q="nothing like it").exists()


def test_the_project_choices_are_the_projects_of_my_requests(
    org: Org, people: People, make_request: MakeRequest
) -> None:
    make_request(Status.SUBMITTED)
    choices = selectors.my_request_projects(AccessContext.for_user(people.tech_mad))
    assert list(choices) == [org.hospital]
    assert not selectors.my_request_projects(AccessContext.for_user(people.partner)).exists()


def test_someone_elses_request_is_not_listed_whatever_the_filter(
    org: Org, people: People, make_request: MakeRequest
) -> None:
    theirs = make_request(Status.SUBMITTED, user=people.partner)
    response = client_for(people.tech_mad).get(
        reverse("requests:list"), {"q": str(theirs.number), "project": str(org.hospital.pk)}
    )
    assert response.status_code == 200
    assert f'data-request="{theirs.pk}"' not in response.content.decode()


def test_the_inbox_sorts_by_priority_needed_by_and_submit_time(
    org: Org, people: People, make_request: MakeRequest
) -> None:
    normal = make_request(Status.SUBMITTED, priority="NORMAL")
    urgent = make_request(Status.SUBMITTED, priority="URGENT")
    ctx = AccessContext.for_user(people.mm_mad)
    default = list(selectors.inbox(ctx))
    assert default == [urgent, normal]
    client = client_for(people.mm_mad)
    descending = client.get(reverse("requests:inbox"), {"sort": "-priority"}).content.decode()
    assert descending.index(str(normal.number)) < descending.index(str(urgent.number))
    ascending = client.get(reverse("requests:inbox"), {"sort": "priority"}).content.decode()
    assert ascending.index(str(urgent.number)) < ascending.index(str(normal.number))
