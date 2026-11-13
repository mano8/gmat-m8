"""The requester's address on the review page (ADR-0029, M9.4): shown to who reviews through
a managing role (group administrator, material manager, engineer), never to a site manager,
who reviews the requests of their projects by name."""

import pytest

from apps.access.context import AccessContext
from apps.accounts.models import User
from apps.accounts.people import NO_NAME
from apps.audit.recorder import AuditIdentity, record
from apps.material_requests import selectors
from apps.material_requests.models import MaterialRequest
from apps.purchasing import selectors as purchasing
from apps.purchasing.models import PurchaseOrder
from tests.orders import order as make_order
from tests.org import Org, People, client_for
from tests.unit.material_requests.conftest import MakeRequest
from tests.unit.material_requests.test_review_workflow_views import html, submitted, url

pytestmark = pytest.mark.django_db

Status = MaterialRequest.Status


@pytest.mark.parametrize(
    ("actor", "shown"),
    [
        ("group_admin", True),
        ("mm_mad", True),
        ("engineer", True),
        ("site_manager", False),
        ("mixed", False),  # a manager of A-SEV, a technician of A-MAD: no A-MAD review
    ],
)
def test_who_sees_the_requesters_address(
    make_request: MakeRequest, people: People, actor: str, shown: bool
) -> None:
    found = make_request(Status.SUBMITTED)
    ctx = AccessContext.for_user(getattr(people, actor))
    assert selectors.shows_requester_address(ctx, found) is shown


@pytest.mark.parametrize(("actor", "shown"), [("mm_mad", True), ("site_manager", False)])
def test_the_review_page(
    make_request: MakeRequest, people: People, actor: str, shown: bool
) -> None:
    found = submitted(make_request, people)
    User.objects.filter(pk=people.tech_mad.pk).update(full_name="Tomás Herrero")
    page = html(client_for(getattr(people, actor), mfa=True).get(url("examine", found)))
    assert "Tomás Herrero" in page
    assert (people.tech_mad.email in page) is shown


class TestHistoriesNameTheActors:
    """The timelines read the audit log, whose label for a person is their address at the
    time: the pages show the person's name instead, and a system actor's label as it is."""

    def actors(self, people: People, found: MaterialRequest, order: PurchaseOrder) -> None:
        for user in (people.tech_mad, people.mm_mad):
            record(
                AuditIdentity(label=user.email, user_id=user.pk),
                "purchase_allocation.trimmed",
                obj=order,
                data={
                    "order_id": str(order.pk),
                    "request_id": str(found.pk),
                    "request_line": 1,
                    "from": "3",
                    "to": "2",
                },
            )
        record(AuditIdentity(label="system:worker"), "purchase_order.issued", obj=order)

    def test_the_order_and_the_requests_trims(
        self, org: Org, make_request: MakeRequest, people: People
    ) -> None:
        found = make_request(Status.APPROVED)
        order = make_order(org, status="ISSUED", issued_by=people.mm_mad)
        User.objects.filter(pk=people.mm_mad.pk).update(full_name="Marta Molina")
        self.actors(people, found, order)
        ctx = AccessContext.for_user(people.mm_mad)
        on_order = [entry.actor for entry in purchasing.history(ctx, order)]
        trims = [trim.actor for trim in purchasing.request_trims(ctx, found)[1]]
        assert on_order == [NO_NAME, "Marta Molina", "system:worker"]
        assert trims == [NO_NAME, "Marta Molina"]
