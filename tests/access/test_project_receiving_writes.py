"""Project receiving written through the URLs, as every actor (M9.3, Q44, ADR-0028).

The rows of ``test_acceptance_matrix`` read every receiving page and send writes that carry no
data. Here the writes are real: each actor of the matrix posts a receipt of 1 on the sentinel
order at the worksite of a project position, in a given state of the organization, and
exactly the actors the rule admits end with a receipt (theirs, with its direct handover and
audit). Everybody else gets a refusal that writes nothing: sign in, 403 (no grant), 404 (not
theirs), or, for who reaches the order but may not use that place, the form again with
"Choose where the goods arrived".

Who is admitted is written from the rule (ADR-0028 §§1-5, plan §3 S1-S9), not read from the
code: a branch role at the worksite of a project its branch runs or buys for, unless the
project is closed; a site manager at an assigned project; a technician at the active projects
of its branch scope; a partner at an assigned active project while their company is active.
The hospital is company B's project (B-MAD runs it) that A-MAD buys for, where the partner of
company C is assigned: the cross-company position. SEV-2 is run by A-SEV and bought by A-MAD:
the operational vs procurement position. The mixed user is a material manager of A-SEV and a
technician of A-MAD (S6). Then the grant changes between the page and the submit, and before
an idempotent replay; and forged places and fields are answered like any other refusal.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.db import transaction
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.catalog.models import Material
from apps.core.idempotency import issue_key
from apps.core.models import DocumentSequence
from apps.handovers.models import Handover
from apps.locations.models import Location
from apps.material_requests.models import MaterialRequest, MaterialRequestLine
from apps.organizations.models import Branch, Company, Membership
from apps.projects.models import Project, ProjectAssignment
from apps.purchasing.models import PurchaseOrderLine
from apps.receiving.models import GoodsReceipt, ReceiptAttribution
from tests.access.matrix import ACTORS, World, send
from tests.access.test_acceptance_matrix import HOSPITAL_RECEIVERS, SEV_2_SITE, WHOLE_ORDER
from tests.orders import allocate, line
from tests.org import session_for

pytestmark = pytest.mark.django_db

USERS = {actor.name: actor.user for actor in ACTORS}
SITE_ONLY = ("site_manager", "tech_mad", "partner")


@pytest.fixture(autouse=True)
def numbers(world: World, db: None) -> None:
    """The world's receipts and handovers carry fixed numbers 1-3 of A-MAD's 2026 sequence;
    the services number theirs after them."""
    year = timezone.now().year
    for kind in ("receipt", "handover"):
        DocumentSequence.objects.update_or_create(
            scope=f"{kind}:A-MAD:{year}", defaults={"next_value": 100}
        )


# --- the organization's states -----------------------------------------------------------


def _status(project: str, status: str) -> Callable[[World], None]:
    def change(world: World) -> None:
        Project.objects.filter(pk=getattr(world.org, project).pk).update(status=status)

    return change


def _company_c_off(world: World) -> None:
    Company.objects.filter(pk=world.org.c.pk).update(is_active=False)


def _b_mad_off(world: World) -> None:
    Branch.objects.filter(pk=world.org.b_mad.pk).update(is_active=False)


def _unchanged(world: World) -> None:
    return None


@dataclass(frozen=True)
class Position:
    project: str  # the project whose worksite the receipt names
    admitted: tuple[str, ...]
    change: Callable[[World], None] = _unchanged


ON_HOLD, CLOSED = Project.Status.ON_HOLD, Project.Status.CLOSED
POSITIONS = {
    # Company B's project bought by A-MAD, the partner of company C assigned to it.
    "hospital": Position("hospital", HOSPITAL_RECEIVERS),
    # Run by A-SEV, bought by A-MAD: A-SEV's manager receives through the project.
    "SEV-2": Position("sev_for_mad", SEV_2_SITE),
    # The order buys nothing for these: its goods cannot arrive there (C7).
    "SEV-1, not on the order": Position("sev_works", ()),
    "MAD-1 on hold, not on the order": Position("mad_hold", ()),
    # Technicians and partners only on active projects; the site manager keeps the assignment.
    "hospital on hold": Position(
        "hospital", (*WHOLE_ORDER, "site_manager"), _status("hospital", ON_HOLD)
    ),
    # The goods of a closed project arrive at a warehouse only (S9).
    "hospital closed": Position("hospital", (), _status("hospital", CLOSED)),
    # The mixed user through their A-SEV manager grant, never through the technician one (S6).
    "SEV-2 on hold": Position(
        "sev_for_mad", (*WHOLE_ORDER, "mm_sev", "mixed"), _status("sev_for_mad", ON_HOLD)
    ),
    # S9: a partner's grants end with their company's activity.
    "hospital, company C inactive": Position(
        "hospital", tuple(a for a in HOSPITAL_RECEIVERS if a != "partner"), _company_c_off
    ),
    # S9: an inactive branch accepts no new documents on its projects.
    "hospital, B-MAD inactive": Position("hospital", (), _b_mad_off),
}


# --- one write -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Counts:
    receipts: int
    handovers: int
    audit: int

    @classmethod
    def now(cls) -> Counts:
        return cls(
            GoodsReceipt.objects.count(),
            Handover.objects.count(),
            AuditEvent.objects.filter(action__startswith="receipt.").count(),
        )

    def plus_one(self) -> Counts:
        return Counts(self.receipts + 1, self.handovers + 1, self.audit + 1)


def order_url(world: World, name: str) -> str:
    return reverse(name, args=[world.objects["purchaseorder"].pk])


def site_of(world: World, project: str) -> Location:
    return world.org.worksite(getattr(world.org, project))


def user_of(world: World, actor: str) -> User:
    return world.user(str(USERS[actor]))


def payload(world: World, actor: str, place: Location | str) -> dict[str, str]:
    """The receipt form of the sentinel order: 1 accepted, at ``place``."""
    line_id = world.objects["purchaseorderline"].pk
    data = {
        "location": str(getattr(place, "pk", place)),
        f"qty_{line_id}": "1",
        f"issue_{line_id}": "NONE",
    }
    if USERS[actor] is not None:
        data["idempotency_key"] = issue_key(user_of(world, actor).pk, "receipt.post")
    return data


def post(client: Client, world: World, data: dict[str, Any]) -> Any:
    return send(client, "POST", order_url(world, "receiving:post"), data)


def refused(response: Any, world: World) -> bool:
    """Answers that tell no more than a 404: 403, 404, sign in, or the form again."""
    if response.status_code in {403, 404}:
        return True
    location = response.get("Location", "").split("?", 1)[0]
    targets = {order_url(world, "receiving:receive"), reverse("account_login")}
    return response.status_code == 302 and location in targets


def admitted_problems(world: World, actor: str, response: Any, before: Counts) -> list[str]:
    """The one receipt is the actor's, so are its direct handover and its audit event."""
    if Counts.now() != before.plus_one():
        return [f"{actor}: wrote {Counts.now()} after {before}, not one receipt"]
    user = user_of(world, actor)
    receipt = GoodsReceipt.objects.latest("created_at")
    handover = Handover.objects.get(goods_receipt=receipt)
    event = AuditEvent.objects.get(action="receipt.posted", object_id=receipt.pk)
    problems = []
    if response.get("Location") != reverse("receiving:detail", args=[receipt.pk]):
        problems.append(f"{actor}: sent to {response.get('Location')}, not to the receipt")
    named = (receipt.received_by_id, handover.handed_by_id, handover.received_by_id, event.actor_id)
    if named != (user.pk,) * 4:
        problems.append(f"{actor}: the receipt, its handover or its audit names someone else")
    return problems


def attempt(world: World, actor: str, data: dict[str, Any], client: Client | None = None) -> Any:
    """One POST whose effects are rolled back: (written, problems)."""
    with transaction.atomic():
        before = Counts.now()
        response = post(client or world.client(actor), world, data)
        written = Counts.now() != before
        if written:
            problems = admitted_problems(world, actor, response, before)
        elif refused(response, world):
            problems = []
        else:
            problems = [f"{actor}: {response.status_code} {response.get('Location', '')}"]
        transaction.set_rollback(True)
    return written, problems


@pytest.mark.parametrize("name", POSITIONS)
def test_who_receives_at_each_position(world: World, name: str) -> None:
    position = POSITIONS[name]
    position.change(world)
    site = site_of(world, position.project)
    problems = []
    for actor in USERS:
        written, found = attempt(world, actor, payload(world, actor, site))
        problems += found
        if written != (actor in position.admitted):
            problems.append(f"{actor}: received {written}, the rule says {not written}")
    assert problems == [], "\n".join(problems)


@pytest.mark.parametrize("name", ["hospital", "SEV-2", "SEV-2 on hold"])
def test_the_mixed_user_without_mfa_is_stepped_up_before_anything(world: World, name: str) -> None:
    """A session of someone holding a privileged role passes MFA before any page (plan §3.6),
    so the mixed user receives nothing without it, not even through the technician grant
    the service alone would accept (``tests/unit/receiving/test_matrix.py``). With MFA, the
    positions above show the two grants never add up: SEV-2 on hold through the A-SEV
    manager grant, never the hospital on hold through the technician one."""
    position = POSITIONS[name]
    position.change(world)
    client = Client()
    client.cookies[settings.SESSION_COOKIE_NAME] = session_for(world.people.mixed, mfa=False)
    page = client.get(order_url(world, "receiving:receive"))
    assert page["Location"].split("?", 1)[0] == reverse("mfa_reauthenticate")
    data = payload(world, "mixed", site_of(world, position.project))
    before = Counts.now()
    assert post(client, world, data).status_code == 403
    assert Counts.now() == before


# --- the grant changes between the page and the submit -----------------------------------


def _revoke_membership(world: World, user: User) -> None:
    Membership.objects.filter(user=user).update(revoked_at=timezone.now())


def _expire_membership(world: World, user: User) -> None:
    Membership.objects.filter(user=user).update(valid_until=timezone.now() - timedelta(seconds=1))


def _revoke_assignment(world: World, user: User) -> None:
    ProjectAssignment.objects.filter(user=user).update(revoked_at=timezone.now())


def _expire_assignment(world: World, user: User) -> None:
    ProjectAssignment.objects.filter(user=user).update(
        valid_until=timezone.now() - timedelta(seconds=1)
    )


def _project(status: str) -> Callable[[World, User], None]:
    return lambda world, user: _status("hospital", status)(world)


def _employer_off(world: World, user: User) -> None:
    _company_c_off(world)


def _password(world: World, user: User) -> None:
    User.objects.filter(pk=user.pk).update(password=make_password("another-pw-1"))


def _deactivate(world: World, user: User) -> None:
    User.objects.filter(pk=user.pk).update(is_active=False)


CHANGES: dict[str, tuple[tuple[str, ...], Callable[[World, User], None]]] = {
    "membership revoked": (("tech_mad", "partner", "site_manager", "wo_mad"), _revoke_membership),
    "membership expired": (("tech_mad", "partner", "mm_mad"), _expire_membership),
    "assignment revoked": (("site_manager", "partner"), _revoke_assignment),
    "assignment expired": (("site_manager", "partner"), _expire_assignment),
    "project on hold": (("tech_mad", "partner"), _project(ON_HOLD)),
    "project closed": (("site_manager", "wo_mad", "engineer"), _project(CLOSED)),
    "employer inactive": (("partner",), _employer_off),
    "password changed": (("tech_mad", "partner"), _password),
    "account deactivated": (("tech_mad", "site_manager"), _deactivate),
}
# A change after which the actor may no longer read the receipt they posted (a closed
# project's receipts stay readable by its site manager and its branch).
LOST_FOR_GOOD = [name for name in CHANGES if name != "project closed"]


def form_payload(world: World, actor: str) -> dict[str, str]:
    """The receipt form as the page gives it to the actor, at the hospital's worksite."""
    site = site_of(world, "hospital")
    url = f"{order_url(world, 'receiving:receive')}?location={site.pk}"
    response = world.client(actor).get(url)
    assert response.status_code == 200, (actor, response.status_code)
    html = response.content.decode()
    line_id = world.objects["purchaseorderline"].pk

    def value(name: str) -> str:
        found = re.search(rf'name="{name}"[^>]*value="([^"]*)"', html)
        assert found is not None, (actor, name)
        return found.group(1)

    return {
        "idempotency_key": value("idempotency_key"),
        "location": value("location"),
        f"qty_{line_id}": value(f"qty_{line_id}"),
        f"issue_{line_id}": "NONE",
    }


def _cases(names: list[str]) -> list[tuple[str, str]]:
    return [(name, actor) for name in names for actor in CHANGES[name][0]]


@pytest.mark.parametrize(("change", "actor"), _cases(list(CHANGES)))
def test_a_change_between_page_load_and_submit_refuses_the_write(
    world: World, change: str, actor: str
) -> None:
    data = form_payload(world, actor)
    assert data["location"] == str(site_of(world, "hospital").pk)
    CHANGES[change][1](world, user_of(world, actor))
    written, problems = attempt(world, actor, data)
    assert (written, problems) == (False, [])


@pytest.mark.parametrize(("change", "actor"), _cases(LOST_FOR_GOOD))
def test_a_change_before_a_replay_refuses_it_and_shows_nothing(
    world: World, change: str, actor: str
) -> None:
    """The first submit posts the receipt; after the change the same submit (a double tap,
    a reload) neither posts again nor leads to the receipt."""
    data = form_payload(world, actor)
    first = post(world.client(actor), world, data)
    receipt = GoodsReceipt.objects.latest("created_at")
    assert first["Location"] == reverse("receiving:detail", args=[receipt.pk])
    CHANGES[change][1](world, user_of(world, actor))
    before = Counts.now()
    again = post(world.client(actor), world, data)
    assert refused(again, world), (again.status_code, again.get("Location"))
    assert Counts.now() == before


def test_a_replay_with_the_grant_intact_leads_to_the_same_receipt(world: World) -> None:
    data = form_payload(world, "partner")
    first = post(world.client("partner"), world, data)
    again = post(world.client("partner"), world, data)
    assert first["Location"] == again["Location"]
    assert GoodsReceipt.objects.filter(received_by=world.people.partner).count() == 1


# --- forged places and fields -------------------------------------------------------------


UNKNOWN = "00000000-0000-7000-8000-000000000000"


@pytest.mark.parametrize("actor", SITE_ONLY)
def test_a_forged_place_is_answered_like_an_unknown_one(world: World, actor: str) -> None:
    """A warehouse, a worksite the order does not serve, a worksite of another project on the
    order, or an id that exists nowhere: the same answer, and nothing written."""
    places: list[Location | str] = [
        UNKNOWN,
        world.org.warehouses["A Madrid"],
        site_of(world, "sev_works"),
        site_of(world, "mad_hold"),
    ]
    if actor != "tech_mad":  # the technician of A-MAD receives at SEV-2 too
        places.append(site_of(world, "sev_for_mad"))
    answers = set()
    for place in places:
        with transaction.atomic():
            before = Counts.now()
            response = post(world.client(actor), world, payload(world, actor, place))
            assert Counts.now() == before
            answers.add((response.status_code, response.get("Location")))
            transaction.set_rollback(True)
    assert answers == {(302, order_url(world, "receiving:receive"))}


@pytest.fixture
def second_line(world: World) -> tuple[PurchaseOrderLine, Any]:
    """A second line on the sentinel order that serves only SEV-2 (another material)."""
    org, people = world.org, world.people
    material = Material.objects.create(
        reference="SENT-02", name="Only for SEV-2", unit=world.objects["material"].unit
    )
    request = MaterialRequest.objects.create(
        project=org.sev_for_mad,
        procurement_branch=org.a_mad,
        requested_by=people.group_admin,
        status=MaterialRequest.Status.APPROVED,
        number="SOL-A-MAD-2026-00091",
        submitted_at=timezone.now(),
        decided_at=timezone.now(),
    )
    request_line = MaterialRequestLine.objects.create(
        request=request,
        project=org.sev_for_mad,
        line_no=1,
        material=material,
        unit=material.unit,
        requested_qty=Decimal(5),
        approved_qty=Decimal(5),
        decision=MaterialRequestLine.Decision.APPROVED,
        decided_by=people.mm_mad,
        decided_at=timezone.now(),
    )
    po_line = line(world.objects["purchaseorder"], material, "5")
    return po_line, allocate(po_line, request_line, "5")


@pytest.mark.parametrize("actor", SITE_ONLY)
def test_forged_fields_for_another_projects_goods_are_ignored(
    world: World, actor: str, second_line: tuple[PurchaseOrderLine, Any]
) -> None:
    """Only the lines and allocations the form offers exist as fields: a quantity for the
    line that serves only SEV-2 and a share of SEV-2's allocation are dropped, and the
    receipt holds the hospital's goods only."""
    po_line, sev_allocation = second_line
    hospital_line = world.objects["purchaseorderline"]
    other_project = world.objects["purchaseallocation"].po_line.allocations.exclude(
        pk=world.objects["purchaseallocation"].pk
    )
    data = form_payload(world, actor) | {
        f"qty_{po_line.pk}": "5",
        f"issue_{po_line.pk}": "NONE",
        f"split_{po_line.pk}_{sev_allocation.pk}": "5",
        **{f"split_{hospital_line.pk}_{a.pk}": "1" for a in other_project},
    }
    response = post(world.client(actor), world, data)
    receipt = GoodsReceipt.objects.latest("created_at")
    assert response["Location"] == reverse("receiving:detail", args=[receipt.pk])
    assert list(receipt.lines.values_list("po_line", flat=True)) == [hospital_line.pk]
    attributed = set(
        ReceiptAttribution.objects.filter(receipt_line__receipt=receipt).values_list(
            "allocation", flat=True
        )
    )
    assert attributed == {world.objects["purchaseallocation"].pk}


# --- the choices offered --------------------------------------------------------------------


def options(html: str, name: str) -> set[str]:
    select = re.search(rf'<select name="{name}".*?</select>', html, re.S)
    return set(re.findall(r'<option value="([^"]+)"', select.group(0))) if select else set()


@pytest.mark.parametrize(
    ("actor", "projects"),
    [
        ("tech_mad", ("hospital", "sev_for_mad")),
        ("mixed", ("hospital", "sev_for_mad")),
        ("mm_sev", ("sev_for_mad",)),
        ("site_manager", ("hospital",)),
        ("partner", ("hospital",)),
    ],
)
def test_the_places_offered_are_the_worksites_of_their_projects(
    world: World, actor: str, projects: tuple[str, ...]
) -> None:
    """The receipt form offers where the order's goods may arrive and the actor may post; a
    project receiver is never offered a warehouse (the form shows one place without a
    choice when there is one)."""
    response = world.client(actor).get(order_url(world, "receiving:receive"))
    html = response.content.decode()
    wanted = {str(site_of(world, project).pk) for project in projects}
    offered = options(html, "location") or set(re.findall(r'name="location" value="([^"]+)"', html))
    assert offered == wanted


@pytest.mark.parametrize("actor", SITE_ONLY)
def test_the_receipts_filter_offers_only_their_worksites(world: World, actor: str) -> None:
    response = world.client(actor).get(reverse("receiving:list"))
    offered = options(response.content.decode(), "place")
    warehouses = {str(place.pk) for place in world.org.warehouses.values()}
    worksites = {
        str(pk)
        for pk in Location.objects.filter(kind=Location.Kind.WORKSITE).values_list("pk", flat=True)
    }
    assert offered and offered <= worksites
    assert not offered & warehouses
