"""Grants, scopes and scoped querysets (apps.access.scopes)."""

from uuid import UUID

import pytest
from django.db.models import Q

from apps.access.roles import Reach, Role
from apps.access.scopes import (
    ALWAYS,
    NEVER,
    Grant,
    Scope,
    ScopedQuerySet,
    any_of,
    object_in_scope,
)
from apps.accounts.models import User
from apps.organizations.models import Branch
from tests.unit.access.conftest import Org

pytestmark = pytest.mark.django_db

USER = UUID(int=9)


def branch_grant(
    *branches: Branch, reach: Reach = Reach.SCOPE, role: Role = Role.TECHNICIAN
) -> Grant:
    return Grant(role, reach, branch_ids=frozenset(branch.pk for branch in branches))


class TestAnyOf:
    def test_no_condition_matches_nothing(self, org: Org) -> None:
        assert any_of([]) is NEVER
        assert not Branch.objects.filter(any_of([])).exists()

    def test_conditions_are_ored(self, org: Org) -> None:
        condition = any_of([Q(pk=org.a_mad.pk), Q(pk=org.a_sev.pk)])
        assert set(Branch.objects.filter(condition)) == {org.a_mad, org.a_sev}

    def test_always_and_never(self, org: Org) -> None:
        assert Branch.objects.filter(ALWAYS).count() == Branch.objects.count()
        assert not Branch.objects.filter(NEVER).exists()


class TestGrant:
    def test_privileged_and_empty(self) -> None:
        assert Grant(Role.MATERIAL_MANAGER, Reach.SCOPE).privileged
        assert not Grant(Role.TECHNICIAN, Reach.SCOPE).privileged
        assert Grant(Role.SITE_MANAGER, Reach.ASSIGNED).is_empty
        assert not Grant(Role.TECHNICIAN, Reach.SCOPE, installation=True).is_empty

    def test_own_documents_need_an_owner_and_a_user(self, org: Org) -> None:
        grant = branch_grant(org.a_mad, reach=Reach.OWN)
        assert grant.document_q(("branch_id",), None, None, USER) is NEVER
        assert grant.document_q(("branch_id",), None, "actor", None) is NEVER


class TestScope:
    def test_bool_and_flags(self, org: Org) -> None:
        assert not Scope("x")
        scope = Scope("x", (branch_grant(org.a_mad),))
        assert scope and not scope.everything and not scope.installation
        everything = Scope("x", (Grant(Role.GROUP_ADMIN, Reach.SCOPE, everything=True),))
        assert everything.everything and everything.installation

    def test_only_splits_privileged_grants(self, org: Org) -> None:
        manager = branch_grant(org.a_sev, role=Role.MATERIAL_MANAGER)
        technician = branch_grant(org.a_mad)
        scope = Scope("x", (manager, technician), USER)
        assert scope.only(privileged=True).grants == (manager,)
        assert scope.only(privileged=False) == Scope("x", (technician,), USER)

    def test_branch_rows(self, org: Org) -> None:
        scope = Scope("x", (branch_grant(org.a_mad), branch_grant(org.a_sev)))
        assert set(Branch.objects.in_scope(scope)) == {org.a_mad, org.a_sev}

    def test_documents_skip_installation_grants(self, org: Org) -> None:
        catalog = Scope("catalog.view", (Grant(Role.TECHNICIAN, Reach.SCOPE, installation=True),))
        assert catalog.document_q(branch="pk") is NEVER

    def test_documents_without_a_project_field_ignore_assignments(self, org: Org) -> None:
        assigned = Grant(
            Role.SITE_MANAGER, Reach.ASSIGNED, project_ids=frozenset({org.hospital.pk})
        )
        assert Scope("x", (assigned,)).document_q(branch="pk") is NEVER

    def test_documents_by_any_of_several_branch_fields(self, org: Org) -> None:
        scope = Scope("x", (branch_grant(org.a_sev),))
        condition = scope.document_q(branch=("branch", "procurement_branch"))
        from apps.projects.models import Project

        assert set(Project.objects.filter(condition).values_list("code", flat=True)) == {
            "SEV-1",
            "SEV-2",
        }


class TestScopedQuerySet:
    def test_an_empty_scope_is_an_empty_queryset(self, org: Org) -> None:
        assert not Branch.objects.in_scope(Scope("x")).exists()

    def test_subclasses_say_how_rows_are_scoped(self) -> None:
        with pytest.raises(NotImplementedError):
            ScopedQuerySet(model=Branch).scope_q(Scope("x"))

    def test_visible_to_asks_the_context(self, org: Org) -> None:
        class Fixed:
            def scope_for(self, capability: str) -> Scope:
                return Scope(capability, (branch_grant(org.b_mad),))

        assert list(Branch.objects.visible_to(Fixed(), "project.view")) == [org.b_mad]

    def test_objects_need_a_scoped_model(self, org: Org) -> None:
        scope = Scope("x", (branch_grant(org.a_mad),))
        assert object_in_scope(org.a_mad, scope) is True
        assert object_in_scope(org.a_mad, Scope("x")) is False
        with pytest.raises(TypeError, match="no scoped queryset"):
            object_in_scope(User(email="u@example.org"), scope)
