"""Every scope-defining change bumps its generation, whatever path makes it (plan §3.6).

The changes are made with plain ORM updates, past the services, to show that the
triggers (``access.0002``, ``accounts.0003``) do the bump. The services' own bumps are
tested with each service.
"""

from collections.abc import Callable
from typing import Any

import pytest
from django.utils import timezone

from apps.access.generations import org_generation
from apps.access.models import AccessState
from apps.access.roles import Role
from apps.accounts.models import User
from apps.organizations.models import Branch, Company, Membership
from apps.projects.models import Project, ProjectAssignment
from tests.support import authz_generation

pytestmark = [pytest.mark.integration, pytest.mark.django_db]


@pytest.fixture
def a() -> Company:
    return Company.objects.create(code="A", legal_name="A")


@pytest.fixture
def mad(a: Company) -> Branch:
    return Branch.objects.create(company=a, code="MAD", name="Madrid")


@pytest.fixture
def sev(a: Company) -> Branch:
    return Branch.objects.create(company=a, code="SEV", name="Sevilla")


@pytest.fixture
def project(mad: Branch) -> Project:
    return Project.objects.create(code="P1", name="P1", branch=mad, procurement_branch=mad)


def bumps(change: Callable[[], Any]) -> int:
    before = org_generation()
    change()
    return org_generation() - before


class TestOrgGeneration:
    def test_creations_bump(self, mad: Branch) -> None:
        assert bumps(lambda: Company.objects.create(code="C", legal_name="C")) == 1
        assert bumps(lambda: Branch.objects.create(company=mad.company, code="X", name="X")) == 1
        assert (
            bumps(
                lambda: Project.objects.create(
                    code="P9", name="P", branch=mad, procurement_branch=mad
                )
            )
            == 1
        )

    @pytest.mark.parametrize(
        ("model", "fixture", "changes"),
        [
            (Company, "a", {"is_active": False}),
            (Branch, "mad", {"is_active": False}),
            (Branch, "sev", {"managed_by": "mad"}),
            (Project, "project", {"status": Project.Status.CLOSED}),
            (Project, "project", {"branch": "sev"}),
            (Project, "project", {"procurement_branch": "sev"}),
        ],
    )
    def test_scope_defining_updates_bump(
        self,
        request: pytest.FixtureRequest,
        model: Any,
        fixture: str,
        changes: dict[str, Any],
    ) -> None:
        obj = request.getfixturevalue(fixture)
        resolved = {
            name: request.getfixturevalue(value) if value in {"mad", "sev"} else value
            for name, value in changes.items()
        }
        assert bumps(lambda: model.objects.filter(pk=obj.pk).update(**resolved)) == 1

    @pytest.mark.parametrize(
        ("model", "fixture", "changes"),
        [
            (Company, "a", {"legal_name": "A S.A."}),
            (Branch, "mad", {"name": "MAD", "allow_self_approval": True}),
            (Project, "project", {"name": "Obra", "client_name": "X"}),
            (Project, "project", {"status": Project.Status.ACTIVE}),  # unchanged value
        ],
    )
    def test_other_updates_do_not(
        self,
        request: pytest.FixtureRequest,
        model: Any,
        fixture: str,
        changes: dict[str, Any],
    ) -> None:
        obj = request.getfixturevalue(fixture)
        assert bumps(lambda: model.objects.filter(pk=obj.pk).update(**changes)) == 0

    def test_a_full_save_without_change_does_not_bump(self, mad: Branch) -> None:
        assert bumps(mad.save) == 0

    def test_the_bump_recreates_a_flushed_row(self) -> None:
        AccessState.objects.all().delete()
        Company.objects.create(code="C", legal_name="C")
        assert org_generation() == 1


class TestAuthzGeneration:
    def test_membership_insert_and_update_bump_the_user(
        self, make_user: Callable[..., User], mad: Branch
    ) -> None:
        user = make_user()
        before = authz_generation(user)
        membership = Membership.objects.create(
            user=user, role=Role.TECHNICIAN, branch=mad, valid_from=timezone.now()
        )
        assert authz_generation(user) == before + 1
        Membership.objects.filter(pk=membership.pk).update(revoked_at=timezone.now())
        assert authz_generation(user) == before + 2

    def test_moving_a_membership_bumps_both_users(
        self, make_user: Callable[..., User], mad: Branch
    ) -> None:
        first, second = make_user("one@example.org"), make_user("two@example.org")
        membership = Membership.objects.create(
            user=first, role=Role.TECHNICIAN, branch=mad, valid_from=timezone.now()
        )
        before = authz_generation(first), authz_generation(second)
        Membership.objects.filter(pk=membership.pk).update(user=second)
        assert (authz_generation(first), authz_generation(second)) == (
            before[0] + 1,
            before[1] + 1,
        )

    def test_assignment_insert_and_update_bump_the_user(
        self, make_user: Callable[..., User], project: Project
    ) -> None:
        user = make_user()
        before = authz_generation(user)
        assignment = ProjectAssignment.objects.create(
            project=project, user=user, kind="MANAGER", valid_from=timezone.now()
        )
        assert authz_generation(user) == before + 1
        ProjectAssignment.objects.filter(pk=assignment.pk).update(valid_until=None)
        assert authz_generation(user) == before + 2

    def test_employer_change_bumps(self, make_user: Callable[..., User], a: Company) -> None:
        user = make_user()
        before = authz_generation(user)
        User.objects.filter(pk=user.pk).update(employer_company=a)
        assert authz_generation(user) == before + 1
        User.objects.filter(pk=user.pk).update(full_name="Ana")
        assert authz_generation(user) == before + 1

    def test_the_counter_still_never_decreases(self, make_user: Callable[..., User]) -> None:
        user = make_user()
        User.objects.filter(pk=user.pk).update(authz_generation=5)
        User.objects.filter(pk=user.pk).update(authz_generation=1, employer_company=None)
        assert authz_generation(user) == 5
