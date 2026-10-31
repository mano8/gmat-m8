"""The acceptance matrix's world: the shared fixture of plan §8.1, built once per module.

Building it per test would dominate the run time (one test per URL), so it is created in
an outer transaction that lives for the module; each test runs in a savepoint inside it
(pytest-django's ``db``), and the outer transaction is rolled back at the end.
"""

from collections.abc import Iterator
from datetime import timedelta

import pytest
from allauth.account.models import EmailAddress
from django.db import transaction
from django.utils import timezone
from pytest_django import DjangoDbBlocker

from apps.accounts.models import EmailChangeRequest, Invitation
from apps.accounts.tokens import new_token
from apps.audit.models import AuditEvent
from apps.audit.recorder import AuditIdentity, record
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
