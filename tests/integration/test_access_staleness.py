"""Race and staleness tests of the access engine (plan M2.3 done-when, §3.6, S10).

Every write runs the full check inside its transaction, whatever the page load showed:

- a revocation committed after the page load makes the write fail;
- a revocation that waits behind a running write applies to every later write;
- a membership that expires between page load and submit denies the write (an expiry
  bumps no counter);
- a password, email or MFA change between page load and submit denies the write;
- a password reset or an offboarding that waits behind a running write ends the session
  for every later write (M8.1);
- a privilege elevation without an MFA step-up denies privileged writes.

The "page load" is the session principal a request builds; the write is a service call.
"""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

import pytest
from allauth.account.forms import default_token_generator
from django.core.exceptions import ValidationError
from django.db import transaction

from apps.access.authorization import authorize_in_txn
from apps.access.exceptions import AccessDenied, MFARequired, NotInScope, StaleSession
from apps.access.principals import SessionPrincipal
from apps.access.roles import Capability, Role
from apps.accounts.adapters import AccountAdapter
from apps.accounts.mfa import reset_mfa
from apps.accounts.models import User
from apps.accounts.services import offboard_user
from apps.core import events
from apps.core.db import db_now
from apps.core.locking import LockOrder
from apps.locations.models import Location
from apps.organizations.models import Branch, Company, Membership
from apps.organizations.services import grant_membership, revoke_membership
from apps.projects.models import Project, ProjectAssignment
from apps.projects.services import assign_to_project, update_project
from tests.integration.conftest import run_concurrently
from tests.support import authz_generation, grant, principal_for

pytestmark = [pytest.mark.integration, pytest.mark.serial, pytest.mark.django_db(transaction=True)]

# Without the membership the project is out of the user's scope: the write answers 404
# (``NotInScope``, S7) rather than 403. Either way it is refused.
DENIED = (AccessDenied, NotInScope)


@dataclass
class World:
    admin: SessionPrincipal
    manager: User
    membership: Membership
    branch: Branch
    project: Project


@pytest.fixture
def world(make_user: Callable[..., User]) -> World:
    """A group administrator, and the material manager of ``A-MAD`` with one project."""
    company = Company.objects.create(code="A", legal_name="Company A")
    branch = Branch.objects.create(company=company, code="MAD", name="Madrid")
    project = Project.objects.create(
        code="P-1", name="Obra 1", branch=branch, procurement_branch=branch
    )
    Location.objects.create(kind=Location.Kind.WORKSITE, branch=branch, project=project, name="P-1")
    admin = make_user("admin@example.org")
    grant(admin, Role.GROUP_ADMIN)
    manager = make_user("mm@example.org")
    membership = grant(manager, Role.MATERIAL_MANAGER, branch=branch)
    return World(principal_for(admin), manager, membership, branch, project)


def write(principal: SessionPrincipal, world: World, name: str = "renamed") -> Project:
    return update_project(principal, world.project.pk, name=name)


def test_a_revocation_committed_after_the_page_load_denies_the_write(world: World) -> None:
    page_load = principal_for(world.manager)
    revoke_membership(world.admin, world.membership.pk)
    with pytest.raises(DENIED):
        write(page_load, world)
    assert Project.objects.get(pk=world.project.pk).name == "Obra 1"


def test_a_revocation_waiting_behind_a_running_write_applies_to_every_later_write(
    world: World,
) -> None:
    page_load = principal_for(world.manager)
    holding = threading.Event()
    timeline: dict[str, float] = {}

    def hold_the_locks(event: events.Event) -> None:
        holding.set()
        time.sleep(0.6)  # the revocation arrives while this write holds its locks
        timeline["write_releases"] = time.monotonic()

    def run(index: int) -> object:
        if index == 0:
            return write(page_load, world, "during")
        assert holding.wait(10)
        timeline["revocation_starts"] = time.monotonic()
        revoked = revoke_membership(world.admin, world.membership.pk)
        timeline["revocation_commits"] = time.monotonic()
        return revoked

    undo = events.subscribe("project.updated", hold_the_locks)
    try:
        written, revoked = run_concurrently(2, run)
    finally:
        undo()
    assert isinstance(written, Project) and isinstance(revoked, Membership)
    # The revocation started while the write ran and committed only after it.
    assert timeline["revocation_starts"] < timeline["write_releases"]
    assert timeline["revocation_commits"] > timeline["write_releases"]
    for later in (page_load, principal_for(world.manager)):
        with pytest.raises(DENIED):
            write(later, world, "after")
    assert Project.objects.get(pk=world.project.pk).name == "during"


def test_an_assignment_waits_for_a_revocation_of_the_assignees_role_and_is_refused(
    world: World, make_user: Callable[..., User]
) -> None:
    """M8.3, R19: the service's check reads the membership under the user-row lock, so it never
    sees a role a transaction in progress is taking away."""
    site = make_user("site@example.org")
    membership = grant(site, Role.SITE_MANAGER, branch=world.branch)
    holding = threading.Event()

    def hold_the_locks(event: events.Event) -> None:
        holding.set()
        time.sleep(0.6)  # the assignment arrives while the revocation holds the user's row

    def run(index: int) -> object:
        if index == 0:
            return revoke_membership(world.admin, membership.pk)
        assert holding.wait(10)
        return assign_to_project(
            world.admin,
            project_id=world.project.pk,
            user_id=site.pk,
            kind=ProjectAssignment.Kind.MANAGER,
        )

    undo = events.subscribe("membership.revoked", hold_the_locks)
    try:
        revoked, refused = run_concurrently(2, run)
    finally:
        undo()
    assert isinstance(revoked, Membership)
    assert isinstance(refused, ValidationError)
    assert not ProjectAssignment.objects.exists()


def reset_password(world: World) -> object:
    """A password reset redeemed through the adapter: under the user-row lock (§3.6)."""
    user = User.objects.get(pk=world.manager.pk)
    token = default_token_generator.make_token(user)
    AccountAdapter().redeem_password_reset(user, token, "a-new-long-password-3")
    return "reset"


def offboard(world: World) -> object:
    return offboard_user(world.admin, world.manager.pk)


@pytest.mark.parametrize(
    "change", [reset_password, offboard], ids=["password-reset", "offboarding"]
)
def test_a_credential_change_waiting_behind_a_running_write_ends_the_session(
    world: World, change: Callable[[World], object]
) -> None:
    """The change locks the user's row ``FOR UPDATE``; the running write holds it ``FOR
    SHARE``, so the change waits for it, and no write of the old session follows."""
    page_load = principal_for(world.manager)
    holding = threading.Event()
    timeline: dict[str, float] = {}

    def hold_the_locks(event: events.Event) -> None:
        holding.set()
        time.sleep(0.6)
        timeline["write_releases"] = time.monotonic()

    def run(index: int) -> object:
        if index == 0:
            return write(page_load, world, "during")
        assert holding.wait(10)
        timeline["change_starts"] = time.monotonic()
        changed = change(world)
        timeline["change_commits"] = time.monotonic()
        return changed

    undo = events.subscribe("project.updated", hold_the_locks)
    try:
        written, changed = run_concurrently(2, run)
    finally:
        undo()
    assert isinstance(written, Project) and not isinstance(changed, Exception), changed
    assert timeline["change_starts"] < timeline["write_releases"] < timeline["change_commits"]
    with pytest.raises((StaleSession, *DENIED)):
        write(page_load, world, "after")
    assert Project.objects.get(pk=world.project.pk).name == "during"


def test_a_membership_that_expires_between_page_load_and_submit_denies_the_write(
    world: World,
) -> None:
    expiry = db_now() + timedelta(seconds=1.5)
    Membership.objects.filter(pk=world.membership.pk).update(valid_until=expiry)
    page_load = principal_for(world.manager)
    write(page_load, world, "before expiry")
    generation = authz_generation(world.manager)
    deadline = time.monotonic() + 10
    while db_now() <= expiry:  # the database clock decides, not this process's
        assert time.monotonic() < deadline
        time.sleep(0.1)
    with pytest.raises(DENIED):
        write(page_load, world, "after expiry")
    assert authz_generation(world.manager) == generation  # an expiry bumps nothing


def change_password(user: User) -> None:
    user.set_password("another-long-password-2")
    user.save(update_fields=["password"])


def change_email(user: User) -> None:
    User.objects.filter(pk=user.pk).update(email="moved@example.org")


@pytest.mark.parametrize(
    "change",
    [change_password, change_email, reset_mfa],
    ids=["password", "email", "mfa-reset"],
)
def test_a_credential_change_between_page_load_and_submit_denies_the_write(
    world: World, change: Callable[[User], None]
) -> None:
    page_load = principal_for(world.manager)
    change(User.objects.get(pk=world.manager.pk))
    with pytest.raises(StaleSession):
        write(page_load, world)
    write(principal_for(world.manager), world)  # a new login works again


def test_privilege_elevation_without_mfa_step_up_denies_privileged_writes(
    world: World, make_user: Callable[..., User]
) -> None:
    technician = make_user("tech@example.org")
    grant(technician, Role.TECHNICIAN, branch=world.branch)
    # The session completed MFA a minute ago, as a technician.
    page_load = principal_for(technician, mfa_at=time.time() - 60)
    grant_membership(
        world.admin, user_id=technician.pk, role=Role.MATERIAL_MANAGER, branch_id=world.branch.pk
    )
    with pytest.raises(MFARequired):
        write(page_load, world)
    with transaction.atomic():  # the technician's own, unprivileged grant still works
        authorize_in_txn(page_load, Capability.REQUEST_CREATE, world.project, locks=LockOrder())
    elevated = User.objects.get(pk=technician.pk).privilege_elevated_at
    assert elevated is not None
    stepped_up = principal_for(technician, mfa_at=elevated.timestamp() + 1)
    assert write(stepped_up, world).name == "renamed"
