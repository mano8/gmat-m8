"""The acceptance matrix's world: the shared fixture of plan §8.1, built once per module.

Building it per test would dominate the run time (one test per URL), so it is created in
an outer transaction that lives for the module; each test runs in a savepoint inside it
(pytest-django's ``db``), and the outer transaction is rolled back at the end.
"""

from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal

import pytest
from allauth.account.models import EmailAddress
from django.db import transaction
from django.utils import timezone
from pytest_django import DjangoDbBlocker

from apps.accounts.models import EmailChangeRequest, Invitation
from apps.accounts.tokens import new_token
from apps.audit.models import AuditEvent
from apps.audit.recorder import AuditIdentity, record
from apps.catalog.models import Material, MaterialCategory, UnitOfMeasure
from apps.material_requests.models import MaterialRequest, MaterialRequestLine
from apps.organizations.models import JointVentureMember, Membership
from apps.projects.models import ProjectAssignment
from tests.access.matrix import World, build_world


def _admin_rows(world: World) -> None:
    """One row of each model the admin lists, for its object URLs."""
    people, org = world.people, world.org
    world.objects["emailaddress"] = EmailAddress.objects.create(
        user=people.tech_mad, email=people.tech_mad.email, primary=True, verified=True
    )
    world.objects["auditevent"] = record(
        AuditIdentity(label=people.group_admin.email, user_id=people.group_admin.pk),
        "fixture.created",
    )
    world.objects["jointventuremember"] = JointVentureMember.objects.create(
        venture=org.b, member=org.a
    )
    unit = UnitOfMeasure.objects.get(code="UD")
    world.objects["unitofmeasure"] = unit
    world.objects["materialcategory"] = MaterialCategory.objects.create(name="Cables")
    world.objects["material"] = Material.objects.create(
        reference="CAB-25",
        name="Cable H07V-K 1x2,5 mm²",
        unit=unit,
        category=world.objects["materialcategory"],
    )
    world.objects["materialrequest"] = request = MaterialRequest.objects.create(
        project=org.hospital, procurement_branch=org.a_mad, requested_by=people.tech_mad
    )
    world.objects["materialrequestline"] = MaterialRequestLine.objects.create(
        request=request,
        project=org.hospital,
        line_no=1,
        material=world.objects["material"],
        unit=unit,
        requested_qty=Decimal("10.000"),
    )
    world.objects["partner_request"] = MaterialRequest.objects.create(
        project=org.hospital, procurement_branch=org.a_mad, requested_by=people.partner
    )
    world.objects["submitted_request"] = submitted = MaterialRequest.objects.create(
        project=org.hospital,
        procurement_branch=org.a_mad,
        requested_by=people.tech_mad,
        status=MaterialRequest.Status.SUBMITTED,
        number="SOL-A-MAD-2026-00001",
        submitted_at=timezone.now(),
    )
    MaterialRequestLine.objects.create(
        request=submitted,
        project=org.hospital,
        line_no=1,
        material=world.objects["material"],
        unit=unit,
        requested_qty=Decimal("3.000"),
    )
    # The reviewer's screens (M4.4): one request per row that may change state, so the
    # order of the rows and actors never matters. All are the technician's, on the hospital.
    material, decided = world.objects["material"], timezone.now()

    def reviewable(key: str, status: str, number: int, *, approved: bool = False) -> None:
        item = MaterialRequest.objects.create(
            project=org.hospital,
            procurement_branch=org.a_mad,
            requested_by=people.tech_mad,
            status=status,
            number=f"SOL-A-MAD-2026-{number:05d}",
            submitted_at=decided,
        )
        decision = (
            {
                "decision": MaterialRequestLine.Decision.APPROVED,
                "approved_qty": Decimal("3.000"),
                "decided_by": people.mm_mad,
                "decided_at": decided,
            }
            if approved
            else {}
        )
        world.objects[key] = item
        world.objects[f"{key}_line"] = MaterialRequestLine.objects.create(
            request=item,
            project=org.hospital,
            line_no=1,
            material=material,
            unit=unit,
            requested_qty=Decimal("3.000"),
            **decision,
        )

    Status = MaterialRequest.Status
    reviewable("review_request", Status.SUBMITTED, 2)
    reviewable("approve_request", Status.SUBMITTED, 3)
    reviewable("cancel_request", Status.SUBMITTED, 4)
    reviewable("approved_request", Status.APPROVED, 5, approved=True)
    world.objects["free_line"] = MaterialRequestLine.objects.create(
        request=world.objects["approved_request"],
        project=org.hospital,
        line_no=2,
        free_text="Cable de obra",
        unit=unit,
        requested_qty=Decimal("2.000"),
        decision=MaterialRequestLine.Decision.APPROVED,
        approved_qty=Decimal("2.000"),
        decided_by=people.mm_mad,
        decided_at=decided,
    )
    world.objects["membership"] = Membership.objects.get(user=people.tech_mad)
    world.objects["projectassignment"] = ProjectAssignment.objects.get(user=people.site_manager)
    later = timezone.now() + timedelta(hours=24)
    world.objects["invitation"] = Invitation.objects.create(
        email="invitee@example.org",
        kind=Invitation.Kind.NEW_ACCOUNT,
        invited_by=people.group_admin,
        token_hash=new_token()[1],
        expires_at=later,
        grants={"memberships": [{"role": "TECHNICIAN", "branch_id": str(org.a_mad.pk)}]},
    )
    world.objects["emailchangerequest"] = EmailChangeRequest.objects.create(
        user=people.tech_mad,
        new_email="tech-new@example.org",
        requested_by=people.group_admin,
        token_hash=new_token()[1],
        cancel_token_hash=new_token()[1],
        expires_at=later,
    )
    assert AuditEvent.objects.exists()


@pytest.fixture(scope="module")
def world(django_db_setup: None, django_db_blocker: DjangoDbBlocker) -> Iterator[World]:
    with django_db_blocker.unblock(), transaction.atomic():
        yield build_world(_admin_rows)
        transaction.set_rollback(True)
